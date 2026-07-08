from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client, config

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
