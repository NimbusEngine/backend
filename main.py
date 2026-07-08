from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client, config
import time

app = FastAPI()

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


@app.get("/")
def health_check():
    return {"status": "ok"}


@app.post("/deployments")
def create_deployment(name: str, image: str):
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
                    host=f"{name}.gsmsv.local",
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

    return {"namespace": namespace, "deployment": name, "status": "created"}


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
    return {"namespace": namespace, "status": "deleted"}

@app.post("/deploy-from-repo")
def deploy_from_repo(name: str, repo_url: str):
    docker_hub_user = "whdudwo1127"
    image = f"{docker_hub_user}/{name}:latest"
    build_job_name = f"kaniko-build-{name}"

    container = client.V1Container(
        name="kaniko",
        image="gcr.io/kaniko-project/executor:latest",
        args=[
            f"--context=git://{repo_url.replace('https://', '')}",
            f"--destination={image}",
        ],
        volume_mounts=[
            client.V1VolumeMount(name="docker-config", mount_path="/kaniko/.docker")
        ]
    )
    pod_spec = client.V1PodSpec(
        containers=[container],
        restart_policy="Never",
        volumes=[
            client.V1Volume(
                name="docker-config",
                secret=client.V1SecretVolumeSource(
                    secret_name="dockerhub-secret",
                    items=[client.V1KeyToPath(key=".dockerconfigjson", path="config.json")]
                )
            )
        ]
    )
    job_body = client.V1Job(
        metadata=client.V1ObjectMeta(name=build_job_name, namespace="default"),
        spec=client.V1JobSpec(
            template=client.V1PodTemplateSpec(spec=pod_spec),
            backoff_limit=0
        )
    )
    # 기존에 같은 이름의 빌드 Job이 있으면 먼저 정리
    try:
        batch_v1.delete_namespaced_job(
            name=build_job_name,
            namespace="default",
            propagation_policy="Foreground"
        )
        time.sleep(3)  # 삭제 완료될 시간 확보
    except client.exceptions.ApiException:
        pass  # 원래 없었으면 그냥 넘어감
    batch_v1.create_namespaced_job(namespace="default", body=job_body)

    # 빌드가 끝날 때까지 대기
    while True:
        job_status = batch_v1.read_namespaced_job_status(name=build_job_name, namespace="default")
        if job_status.status.succeeded:
            break
        if job_status.status.failed:
            return {"status": "build failed"}
        time.sleep(3)

    # 빌드 끝났으니 기존 배포 로직 재사용
    return create_deployment(name=name, image=image)
