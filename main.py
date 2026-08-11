from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client, config
import time
import sqlite3
import requests
import pymysql
import bcrypt
import secrets
import re
from datetime import datetime, timezone

app = FastAPI()

PUBLIC_HOST = "158.247.251.109"
PUBLIC_PORT = 26117
DOCKER_HUB_USER = "whdudwo1127"

MYSQL_PASSWORD = "whdudwo1127"

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


# ---------- SQLite (배포 기록 로그, 기존 그대로) ----------

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


# ---------- MySQL (사용자 계정, 배포 소유권) ----------

def get_db():
    return pymysql.connect(
        host="localhost",
        user="nimbus",
        password=MYSQL_PASSWORD,
        database="nimbusengine",
        cursorclass=pymysql.cursors.DictCursor
    )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def generate_api_key() -> str:
    return "nimbus_" + secrets.token_hex(16)


def validate_name(name: str):
    if not re.match(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', name):
        raise HTTPException(
            status_code=400,
            detail="프로젝트 이름은 소문자/숫자/하이픈(-)만 사용 가능하고, 문자나 숫자로 시작·끝나야 합니다. (예: my-app)"
        )


def get_current_user(x_api_key: str = Header(None)) -> str:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API 키가 필요합니다")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE api_key=%s", (x_api_key,))
            user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid API Key")
        return user["username"]
    finally:
        conn.close()


@app.post("/auth/register")
def register(username: str, password: str):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username=%s", (username,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="이미 존재하는 아이디입니다")
            api_key = generate_api_key()
            cur.execute(
                "INSERT INTO users (username, password_hash, api_key) VALUES (%s, %s, %s)",
                (username, hash_password(password), api_key)
            )
        conn.commit()
        return {"username": username, "api_key": api_key}
    finally:
        conn.close()


@app.post("/auth/login")
def login(username: str, password: str):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username=%s", (username,))
            user = cur.fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 틀렸습니다")
        return {"username": username, "api_key": user["api_key"]}
    finally:
        conn.close()


def record_ownership(name, owner, source, detail):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "REPLACE INTO deployments (name, owner, source, detail) VALUES (%s, %s, %s, %s)",
                (name, owner, source, detail)
            )
        conn.commit()
    finally:
        conn.close()


def get_owner(name):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT owner FROM deployments WHERE name=%s", (name,))
            row = cur.fetchone()
        return row["owner"] if row else None
    finally:
        conn.close()


def remove_ownership(name):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM deployments WHERE name=%s", (name,))
        conn.commit()
    finally:
        conn.close()


# ---------- GitHub 저장소 자동 감지 ----------

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


DEFAULT_PORT = 80

# Dockerfile을 자동 생성하는 경우, 프로젝트 종류별로 애플리케이션이 실제로 듣는 포트
AUTO_PORTS = {
    "node": 3000,      # npm start (Express 등 다수가 3000)
    "django": 80,      # runserver 0.0.0.0:80 으로 고정해 생성
    "python": 5000,    # Flask 기본 포트
    "static": 80,      # nginx
}


def detect_project_type(files):
    if "package.json" in files:
        return "node"
    if "requirements.txt" in files:
        return "django" if "manage.py" in files else "python"
    if "index.html" in files:
        return "static"
    return None


def detect_port(owner, repo, files):
    """배포할 컨테이너가 실제로 listen 하는 포트를 추정한다.

    Dockerfile이 있으면 EXPOSE 값을 사용하고(멀티스테이지면 마지막 값),
    없으면 자동 생성할 Dockerfile의 프로젝트 종류별 기본 포트를 사용한다.
    """
    if "Dockerfile" in files:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/Dockerfile"
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                exposed = re.findall(r"^\s*EXPOSE\s+(\d+)", res.text, re.MULTILINE | re.IGNORECASE)
                if exposed:
                    return int(exposed[-1])
        except requests.RequestException:
            pass
        return DEFAULT_PORT

    return AUTO_PORTS.get(detect_project_type(files), DEFAULT_PORT)


def generate_dockerfile(files):
    if "package.json" in files:
        return (
            "FROM node:20-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN npm install\n"
            f"ENV PORT={AUTO_PORTS['node']}\n"
            f"EXPOSE {AUTO_PORTS['node']}\n"
            'CMD ["npm", "start"]\n'
        )
    if "requirements.txt" in files:
        if "manage.py" in files:
            port = AUTO_PORTS["django"]
            return (
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY . .\n"
                "RUN pip install -r requirements.txt\n"
                f"EXPOSE {port}\n"
                f'CMD ["python", "manage.py", "runserver", "0.0.0.0:{port}"]\n'
            )
        entry = "app.py" if "app.py" in files else "main.py"
        return (
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN pip install -r requirements.txt\n"
            f"EXPOSE {AUTO_PORTS['python']}\n"
            f'CMD ["python", "{entry}"]\n'
        )
    if "index.html" in files:
        return (
            "FROM nginx:alpine\n"
            "COPY . /usr/share/nginx/html\n"
            f"EXPOSE {AUTO_PORTS['static']}\n"
        )
    return None


@app.get("/")
def health_check():
    return {"status": "ok"}


# ---------- 프로젝트 / 컴포넌트 배포 ----------
#
# 배포 단위는 "프로젝트"이고, 프로젝트 하나가 네임스페이스 하나(user-<project>)에 대응한다.
# 프로젝트 안에는 컴포넌트를 여러 개 둘 수 있으며, 같은 네임스페이스에 있으므로
# 컴포넌트끼리 Service 이름으로 서로를 호출할 수 있다.
#   예) 프론트엔드에서 http://<service_name> 으로 백엔드 호출
#
# 외부 노출은 expose=True 인 컴포넌트에만 Ingress를 만들어 처리한다.


def project_namespace(project: str) -> str:
    return f"user-{project}"


def project_url(project: str) -> str:
    return f"http://{project}.{PUBLIC_HOST}.sslip.io:{PUBLIC_PORT}"


def component_slug(project: str, component: str) -> str:
    """이미지 태그와 빌드 Job 이름에 쓰는 식별자.

    단일 컴포넌트 프로젝트에서 이름이 중복되지 않도록(taskflow-taskflow) 구분한다.
    """
    return component if project == component else f"{project}-{component}"


def ensure_namespace(namespace: str):
    """네임스페이스가 없으면 만들고, 이미 있으면 그대로 사용한다."""
    try:
        v1.read_namespace(name=namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        v1.create_namespace(body=client.V1Namespace(
            metadata=client.V1ObjectMeta(name=namespace)
        ))


def apply_deployment(namespace, name, image, port):
    container = client.V1Container(
        name=name,
        image=image,
        ports=[client.V1ContainerPort(container_port=port)]
    )
    body = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": name}),
                spec=client.V1PodSpec(containers=[container])
            )
        )
    )
    try:
        apps_v1.create_namespaced_deployment(namespace=namespace, body=body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        apps_v1.replace_namespaced_deployment(name=name, namespace=namespace, body=body)


def apply_service(namespace, service_name, app_label, port):
    body = client.V1Service(
        metadata=client.V1ObjectMeta(name=service_name, namespace=namespace),
        spec=client.V1ServiceSpec(
            selector={"app": app_label},
            ports=[client.V1ServicePort(port=80, target_port=port)]
        )
    )
    try:
        v1.create_namespaced_service(namespace=namespace, body=body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        # Service는 clusterIP가 불변이라 교체 대신 spec만 갱신한다
        v1.patch_namespaced_service(name=service_name, namespace=namespace, body=body)


def apply_ingress(namespace, project, service_name):
    """프로젝트의 진입점 Ingress. 호스트는 프로젝트 이름 하나로 고정한다."""
    body = client.V1Ingress(
        metadata=client.V1ObjectMeta(
            name=project,
            namespace=namespace,
            annotations={"nginx.ingress.kubernetes.io/rewrite-target": "/"}
        ),
        spec=client.V1IngressSpec(
            ingress_class_name="nginx",
            rules=[
                client.V1IngressRule(
                    host=f"{project}.{PUBLIC_HOST}.sslip.io",
                    http=client.V1HTTPIngressRuleValue(
                        paths=[
                            client.V1HTTPIngressPath(
                                path="/",
                                path_type="Prefix",
                                backend=client.V1IngressBackend(
                                    service=client.V1IngressServiceBackend(
                                        name=service_name,
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
    try:
        networking_v1.create_namespaced_ingress(namespace=namespace, body=body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise
        networking_v1.replace_namespaced_ingress(name=project, namespace=namespace, body=body)


def deploy_component(project, name, image, port=DEFAULT_PORT, service_name=None,
                     expose=True, source="image", username="unknown"):
    validate_name(project)
    validate_name(name)

    namespace = project_namespace(project)
    service_name = service_name or name
    validate_name(service_name)

    ensure_namespace(namespace)
    apply_deployment(namespace, name, image, port)
    apply_service(namespace, service_name, name, port)
    if expose:
        apply_ingress(namespace, project, service_name)

    record_ownership(project, username, source, image)
    log_history(project, source, "created", f"{name} <- {image} (:{port})")

    return {
        "project": project,
        "namespace": namespace,
        "component": name,
        "service": service_name,
        "port": port,
        "exposed": expose,
        "status": "created",
        "url": project_url(project) if expose else None,
    }


@app.post("/deployments")
def create_deployment(name: str, image: str, source: str = "image", port: int = DEFAULT_PORT,
                      username: str = Depends(get_current_user)):
    """이미지 하나를 그대로 배포한다. 프로젝트 이름과 컴포넌트 이름이 같은 단일 구성."""
    return deploy_component(
        project=name, name=name, image=image, port=port,
        service_name=name, expose=True, source=source, username=username
    )


@app.get("/projects/{project}")
def get_project(project: str):
    namespace = project_namespace(project)
    try:
        deployments = apps_v1.list_namespaced_deployment(namespace=namespace)
    except client.exceptions.ApiException:
        return {"error": "not found"}

    services = v1.list_namespaced_service(namespace=namespace)
    service_names = [s.metadata.name for s in services.items]

    return {
        "project": project,
        "namespace": namespace,
        "url": project_url(project),
        "components": [
            {
                "name": d.metadata.name,
                "ready_replicas": d.status.ready_replicas or 0,
                "replicas": d.status.replicas or 0,
                "image": d.spec.template.spec.containers[0].image,
            }
            for d in deployments.items
        ],
        "services": service_names,
    }


@app.delete("/projects/{project}")
def delete_project(project: str, username: str = Depends(get_current_user)):
    owner = get_owner(project)
    if owner and owner != username:
        raise HTTPException(status_code=403, detail="본인이 만든 프로젝트만 삭제할 수 있습니다")

    v1.delete_namespace(name=project_namespace(project))
    remove_ownership(project)
    log_history(project, "project", "deleted")
    return {"project": project, "status": "deleted"}


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
def delete_deployment(name: str, username: str = Depends(get_current_user)):
    owner = get_owner(name)
    if owner and owner != username:
        raise HTTPException(status_code=403, detail="본인이 만든 배포만 삭제할 수 있습니다")

    namespace = f"user-{name}"
    v1.delete_namespace(name=namespace)
    remove_ownership(name)
    log_history(name, "unknown", "deleted")
    return {"namespace": namespace, "status": "deleted"}


@app.post("/deploy-from-repo")
def deploy_from_repo(
    name: str,
    repo_url: str,
    project: str = None,
    service_name: str = None,
    expose: bool = True,
    username: str = Depends(get_current_user),
):
    """저장소를 빌드해 컴포넌트로 배포한다.

    project를 생략하면 name과 같은 이름의 단일 컴포넌트 프로젝트가 된다.
    같은 project로 여러 번 호출하면 한 네임스페이스에 컴포넌트가 쌓이고,
    컴포넌트끼리는 service_name으로 서로를 호출할 수 있다.
    """
    validate_name(name)
    project = project or name
    validate_name(project)
    service_name = service_name or name
    validate_name(service_name)

    slug = component_slug(project, name)
    image = f"{DOCKER_HUB_USER}/{slug}:latest"
    build_job_name = f"kaniko-build-{slug}"
    configmap_name = f"dockerfile-{slug}"

    owner, repo = parse_repo_url(repo_url)
    files = get_repo_files(owner, repo)
    has_dockerfile = "Dockerfile" in files

    # 배포 단계에서 다시 조회하지 않도록, 빌드 Job 어노테이션에 포트를 남겨둔다
    app_port = detect_port(owner, repo, files)

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
        metadata=client.V1ObjectMeta(
            name=build_job_name,
            namespace="default",
            annotations={
                "nimbus.io/owner": username,
                "nimbus.io/port": str(app_port),
                "nimbus.io/project": project,
                "nimbus.io/component": name,
                "nimbus.io/service": service_name,
                "nimbus.io/expose": str(expose).lower(),
            }
        ),
        spec=client.V1JobSpec(
            template=client.V1PodTemplateSpec(spec=pod_spec),
            backoff_limit=0,
            ttl_seconds_after_finished=300
        )
    )
    batch_v1.create_namespaced_job(namespace="default", body=job_body)

    return {
        "status": "building",
        "project": project,
        "name": name,
        "service": service_name,
        "port": app_port,
        "exposed": expose,
    }


@app.get("/deploy-from-repo/{name}/status")
def deploy_from_repo_status(name: str, project: str = None):
    project = project or name
    build_job_name = f"kaniko-build-{component_slug(project, name)}"

    try:
        job = batch_v1.read_namespaced_job(name=build_job_name, namespace="default")
    except client.exceptions.ApiException:
        return {"status": "not found"}

    job_status = job.status
    annotations = job.metadata.annotations or {}
    owner = annotations.get("nimbus.io/owner", "unknown")
    app_port = int(annotations.get("nimbus.io/port", DEFAULT_PORT))
    project = annotations.get("nimbus.io/project", project)
    component = annotations.get("nimbus.io/component", name)
    service_name = annotations.get("nimbus.io/service", component)
    expose = annotations.get("nimbus.io/expose", "true") == "true"

    image = f"{DOCKER_HUB_USER}/{component_slug(project, component)}:latest"

    if job_status.failed:
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
        log_history(project, "repo", "build_failed", logs[:300])
        return {"status": "build failed", "logs": logs}

    if not job_status.succeeded:
        return {"status": "building"}

    # 빌드 성공 -> 이미 배포돼 있으면 그대로 두고, 없으면 배포한다
    namespace = project_namespace(project)
    try:
        apps_v1.read_namespaced_deployment(name=component, namespace=namespace)
        return {
            "project": project,
            "namespace": namespace,
            "component": component,
            "status": "created",
            "url": project_url(project) if expose else None,
        }
    except client.exceptions.ApiException:
        return deploy_component(
            project=project, name=component, image=image, port=app_port,
            service_name=service_name, expose=expose, source="repo", username=owner
        )


@app.get("/history")
def get_history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(row) for row in rows]
