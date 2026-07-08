from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client, config
import time
import sqlite3
import requests
from datetime import datetime, timezone

app = FastAPI()

PUBLIC_HOST = "158.247.251.109"
PUBLIC_PORT = 26117
DOCKER_HUB_USER = "whdudwo1127"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


config.load_kube_config()
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
batch_v1 = client.BatchV1Api()
networking_v1 = client.NetworkingV1Api()


DB_PATH = "/home/ubuntu/backend/history.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def log_history(name, source, status, detail=""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO history (name, source, status, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, source, status, detail, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


init_db()


def parse_repo_url(repo_url):
    path = repo_url.replace("https://github.com/", "").replace(".git", "").strip("/")
    parts = path.split("/")
    return parts[0], parts[1]


def get_repo_files(owner, repo):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/"
    res = requests.get(url, timeout=10)
    if res.status_code != 200:
        return []
    return [item["name"] for item in res.json()]


def generate_dockerfile(files):
    if "package.json" in files:
        return (
            "FROM node:20-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN npm install\n"
            'CMD ["npm", "start"]\n'
        )
    if "requirements.txt" in files:
        if "manage.py" in files:
            return (
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY . .\n"
                "RUN pip install -r requirements.txt\n"
                'CMD ["python", "manage.py", "runserver", "0.0.0.0:80"]\n'
            )
        entry = "app.py" if "app.py" in files else "main.py"
        return (
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN pip install -r requirements.txt\n"
            f'CMD ["python", "{entry}"]\n'
        )
    if "index.html" in files:
        return (
            "FROM nginx:alpine\n"
            "COPY . /usr/share/nginx/html\n"
        )
    return None


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.post("/deployments")
def create_deployment(name: str, image: str, source: str = "image"):
    namespace = f"user-{name}"

    # 1. 네임스페이스 생성
    ns_body = client.V1Namespace(
        metadata=client.V1ObjectMeta(name=namespace)
    )
    v1.create_namespace(body=ns_body)

    # 2. Deployment 생성
    container = client.V1Container(
        name=name,
        image=image,
        ports=[client.V1ContainerPort(container_port=80)]
    )
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": name}),
        spec=client.V1PodSpec(containers=[container])
    )
    spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels={"app": name}),
        template=template
    )
    deployment_body = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=spec
    )
    apps_v1.create_namespaced_deployment(namespace=namespace, body=deployment_body)

    # 3. Service 생성
    service_body = client.V1Service(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1ServiceSpec(
            selector={"app": name},
            ports=[client.V1ServicePort(port=80, target_port=80)]
        )
    )
    v1.create_namespaced_service(namespace=namespace, body=service_body)

    # 4. Ingress 생성
    ingress_body = client.V1Ingress(
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=namespace,
            annotations={"nginx.ingress.kubernetes.io/rewrite-target": "/"}
        ),
        spec=client.V1IngressSpec(
            ingress_class_name="nginx",
            rules=[
                client.V1IngressRule(
                    host=f"{name}.{PUBLIC_HOST}.sslip.io",
                    http=client.V1HTTPIngressRuleValue(
                        paths=[
                            client.V1HTTPIngressPath(
                                path="/",
                                path_type="Prefix",
                                backend=client.V1IngressBackend(
                                    service=client.V1IngressServiceBackend(
                                        name=name,
                                        port=client.V1ServiceBackendPort(number=80)
                                    )
                                )
                            )
                        ]
                    )
                )
            ]
        )
    )
    networking_v1.create_namespaced_ingress(namespace=namespace, body=ingress_body)

    log_history(name, source, "created", image)

    return {
        "namespace": namespace,
        "deployment": name,
        "status": "created",
        "url": f"http://{name}.{PUBLIC_HOST}.sslip.io:{PUBLIC_PORT}"
    }


@app.get("/deployments/{name}")
def get_deployment(name: str):
    namespace = f"user-{name}"
    try:
        deployment = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        return {
            "namespace": namespace,
            "ready_replicas": deployment.status.ready_replicas,
            "replicas": deployment.status.replicas
        }
    except client.exceptions.ApiException:
        return {"error": "not found"}


@app.delete("/deployments/{name}")
def delete_deployment(name: str):
    namespace = f"user-{name}"
    v1.delete_namespace(name=namespace)
    log_history(name, "unknown", "deleted")
    return {"namespace": namespace, "status": "deleted"}


@app.post("/deploy-from-repo")
def deploy_from_repo(name: str, repo_url: str):
    image = f"{DOCKER_HUB_USER}/{name}:latest"
    build_job_name = f"kaniko-build-{name}"
    configmap_name = f"dockerfile-{name}"

    owner, repo = parse_repo_url(repo_url)
    files = get_repo_files(owner, repo)
    has_dockerfile = "Dockerfile" in files

    # 기존에 같은 이름의 빌드 Job/ConfigMap이 있으면 먼저 정리
    try:
        batch_v1.delete_namespaced_job(
            name=build_job_name,
            namespace="default",
            propagation_policy="Foreground"
        )
        time.sleep(3)
    except client.exceptions.ApiException:
        pass

    try:
        v1.delete_namespaced_config_map(name=configmap_name, namespace="default")
    except client.exceptions.ApiException:
        pass

    docker_config_volume = client.V1Volume(
        name="docker-config",
        secret=client.V1SecretVolumeSource(
            secret_name="dockerhub-secret",
            items=[client.V1KeyToPath(key=".dockerconfigjson", path="config.json")]
        )
    )
    docker_config_mount = client.V1VolumeMount(name="docker-config", mount_path="/kaniko/.docker")

    if has_dockerfile:
        # 저장소에 Dockerfile이 이미 있으면 -> 기존 방식(git context) 그대로
        kaniko_container = client.V1Container(
            name="kaniko",
            image="gcr.io/kaniko-project/executor:latest",
            args=[
                f"--context=git://{repo_url.replace('https://', '')}",
                f"--destination={image}",
            ],
            volume_mounts=[docker_config_mount]
        )
        pod_spec = client.V1PodSpec(
            containers=[kaniko_container],
            restart_policy="Never",
            volumes=[docker_config_volume]
        )
    else:
        # Dockerfile이 없으면 -> 파일 목록 보고 자동 생성
        dockerfile_content = generate_dockerfile(files)
        if dockerfile_content is None:
            log_history(name, "repo", "build_failed", "Dockerfile 없음, 자동 감지 실패")
            return {
                "status": "build failed",
                "logs": "Dockerfile이 없고, requirements.txt / package.json / index.html 중 아무것도 없어서 어떤 프로젝트인지 자동으로 판단할 수 없습니다."
            }

        cm_body = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=configmap_name, namespace="default"),
            data={"Dockerfile": dockerfile_content}
        )
        v1.create_namespaced_config_map(namespace="default", body=cm_body)

        workspace_volume = client.V1Volume(name="workspace", empty_dir=client.V1EmptyDirVolumeSource())
        workspace_mount = client.V1VolumeMount(name="workspace", mount_path="/workspace")

        dockerfile_volume = client.V1Volume(
            name="dockerfile-src",
            config_map=client.V1ConfigMapVolumeSource(name=configmap_name)
        )
        dockerfile_mount = client.V1VolumeMount(name="dockerfile-src", mount_path="/dockerfile-src")

        clone_container = client.V1Container(
            name="clone",
            image="alpine/git",
            args=["clone", "--depth=1", repo_url, "/workspace"],
            volume_mounts=[workspace_mount]
        )
        inject_container = client.V1Container(
            name="inject-dockerfile",
            image="busybox",
            command=["sh", "-c", "cp /dockerfile-src/Dockerfile /workspace/Dockerfile"],
            volume_mounts=[workspace_mount, dockerfile_mount]
        )
        kaniko_container = client.V1Container(
            name="kaniko",
            image="gcr.io/kaniko-project/executor:latest",
            args=[
                "--context=dir:///workspace",
                f"--destination={image}",
            ],
            volume_mounts=[workspace_mount, docker_config_mount]
        )
        pod_spec = client.V1PodSpec(
            init_containers=[clone_container, inject_container],
            containers=[kaniko_container],
            restart_policy="Never",
            volumes=[workspace_volume, dockerfile_volume, docker_config_volume]
        )

    job_body = client.V1Job(
        metadata=client.V1ObjectMeta(name=build_job_name, namespace="default"),
        spec=client.V1JobSpec(
            template=client.V1PodTemplateSpec(spec=pod_spec),
            backoff_limit=0,
            ttl_seconds_after_finished=300
        )
    )
    batch_v1.create_namespaced_job(namespace="default", body=job_body)

    return {"status": "building", "name": name}


@app.get("/deploy-from-repo/{name}/status")
def deploy_from_repo_status(name: str):
    image = f"{DOCKER_HUB_USER}/{name}:latest"
    build_job_name = f"kaniko-build-{name}"

    try:
        job_status = batch_v1.read_namespaced_job_status(name=build_job_name, namespace="default")
    except client.exceptions.ApiException:
        return {"status": "not found"}

    if job_status.status.failed:
        pods = v1.list_namespaced_pod(namespace="default", label_selector=f"job-name={build_job_name}")
        logs = "로그를 찾을 수 없음"
        if pods.items:
            pod_name = pods.items[0].metadata.name
            for container_name in ["kaniko", "inject-dockerfile", "clone"]:
                try:
                    logs = v1.read_namespaced_pod_log(name=pod_name, namespace="default", container=container_name)
                    break
                except client.exceptions.ApiException:
                    continue
        log_history(name, "repo", "build_failed", logs[:300])
        return {"status": "build failed", "logs": logs}

    if not job_status.status.succeeded:
        return {"status": "building"}

    # 빌드 성공 -> 이미 배포됐는지 확인 후, 없으면 새로 배포
    namespace = f"user-{name}"
    try:
        apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
        return {
            "namespace": namespace,
            "deployment": name,
            "status": "created",
            "url": f"http://{name}.{PUBLIC_HOST}.sslip.io:{PUBLIC_PORT}"
        }
    except client.exceptions.ApiException:
        return create_deployment(name=name, image=image, source="repo")


@app.get("/history")
def get_history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(row) for row in rows]

