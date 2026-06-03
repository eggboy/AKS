#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "kubernetes>=31.0.0",
#     "pyyaml>=6.0.2",
#     "tyro>=0.9",
# ]
# ///
"""Generate kubeconfig files for Kubernetes ServiceAccounts.

This tool allows you to easily create kubeconfig files for ServiceAccounts
in your Kubernetes cluster. This enables applications or users to authenticate
to Kubernetes using a ServiceAccount identity and its associated RBAC permissions.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import tyro
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


class KubeconfigGeneratorError(Exception):
    """Raised when a kubeconfig cannot be generated from the available inputs."""


def default_kubeconfig_path() -> str:
    """Get the default kubeconfig path."""
    home = Path.home()
    if home:
        return str(home / ".kube" / "config")
    return ""


@dataclass
class Config:
    """Generate kubeconfig files for Kubernetes ServiceAccounts."""

    service_account_name: Annotated[str, tyro.conf.arg(aliases=["-sa"], metavar="NAME")]
    """Name of the ServiceAccount."""
    namespace: Annotated[str, tyro.conf.arg(aliases=["-n"], metavar="NS")] = "default"
    """Namespace of the ServiceAccount."""
    output_path: Annotated[str, tyro.conf.arg(aliases=["-o"], metavar="PATH")] = "auto"
    """Output path for the kubeconfig file. Auto-derived as kubeconfig-<sa-name>."""
    context_name: Annotated[str, tyro.conf.arg(metavar="NAME")] = "auto"
    """Context name in kubeconfig. Auto-derived from service account name."""
    cluster_name: Annotated[str, tyro.conf.arg(metavar="NAME")] = "auto"
    """Cluster name in kubeconfig. Auto-detected from current context."""
    api_server: Annotated[str, tyro.conf.arg(metavar="URL")] = "auto"
    """API server URL. Auto-detected from current context."""
    kubeconfig_path: Annotated[str, tyro.conf.arg(metavar="PATH")] = field(default_factory=default_kubeconfig_path)
    """Path to the kubeconfig file."""
    token_expiry_hours: Annotated[int, tyro.conf.arg(metavar="HOURS")] = 8760
    """TokenRequest expiry in hours, used only when falling back to `kubectl create token`."""
    prefer_long_lived_token: Annotated[bool, tyro.conf.arg(metavar="BOOL")] = True
    """Prefer the SA's long-lived `kubernetes.io/service-account-token` Secret (ArgoCD's documented model).

    When True (default), look up a token from the SA's referenced Secret and only
    fall back to `kubectl create token` (TokenRequest, ≤1y) if no such Secret
    exists. When False, the legacy ordering is used: TokenRequest first, Secret
    only as a fallback.
    """


def load_kubeconfig_file(kubeconfig_path: str) -> dict[str, Any]:
    """Load kubeconfig from file."""
    with open(kubeconfig_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_kubeconfig_file(kubeconfig: dict[str, Any], output_path: str) -> None:
    """Save kubeconfig to file with 0600 permissions."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, "w", encoding="utf-8") as f:
        yaml.safe_dump(kubeconfig, f, default_flow_style=False)

    output.chmod(0o600)


SA_TOKEN_SECRET_TYPE = "kubernetes.io/service-account-token"  # noqa: S105 - Kubernetes Secret type identifier, not a credential
SA_NAME_ANNOTATION = "kubernetes.io/service-account.name"


def _decode_token(secret: client.V1Secret) -> str | None:
    """Return the decoded bearer token from a service-account-token Secret, or None."""
    if secret.type != SA_TOKEN_SECRET_TYPE:
        return None
    data = secret.data or {}
    token = data.get("token")
    if not token:
        return None
    return base64.b64decode(token).decode("utf-8")


def get_token_from_secret(v1: client.CoreV1Api, cfg: Config) -> str:
    """Return a long-lived token from the ServiceAccount's token Secret.

    Looks for a token in this order:
      1. Secrets referenced by `serviceaccount.secrets[]` whose type is
         `kubernetes.io/service-account-token`.
      2. Any Secret in the SA's namespace annotated with
         `kubernetes.io/service-account.name=<sa>` (covers the modern case
         where the SA does not auto-reference its token Secret).

    Raises:
        KubeconfigGeneratorError: if no populated token Secret is found.
    """
    sa = v1.read_namespaced_service_account(cfg.service_account_name, cfg.namespace)

    for ref in sa.secrets or []:
        try:
            secret = v1.read_namespaced_secret(ref.name, cfg.namespace)
        except ApiException:
            continue
        token = _decode_token(secret)
        if token:
            return token

    secret_list = v1.list_namespaced_secret(cfg.namespace)
    for secret in secret_list.items:
        if secret.type != SA_TOKEN_SECRET_TYPE:
            continue
        annotations = (secret.metadata.annotations or {}) if secret.metadata else {}
        if annotations.get(SA_NAME_ANNOTATION) != cfg.service_account_name:
            continue
        token = _decode_token(secret)
        if token:
            return token

    raise KubeconfigGeneratorError(
        f"No populated service-account-token Secret found for "
        f"{cfg.namespace}/{cfg.service_account_name}. Create one with "
        f"`type: kubernetes.io/service-account-token` and the "
        f"`{SA_NAME_ANNOTATION}` annotation, then wait for the TokenController "
        f"to populate `.data.token`."
    )


def create_token_with_kubectl(cfg: Config) -> str | None:
    """Try to create a short-lived token using `kubectl create token` (TokenRequest API).

    Returns the token string on success, or None if kubectl is unavailable or
    the cluster rejected the request (e.g. on older Kubernetes versions).
    """
    args = ["kubectl", "create", "token", cfg.service_account_name, "-n", cfg.namespace]

    if cfg.kubeconfig_path:
        args.append(f"--kubeconfig={cfg.kubeconfig_path}")

    args.append(f"--duration={cfg.token_expiry_hours}h")

    try:
        # subprocess.run with a fixed argv list (no shell=True); kubectl is a
        # known binary and values come from validated CLI flags.
        result = subprocess.run(args, capture_output=True, text=True, check=True)  # noqa: S603
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_service_account_token(v1: client.CoreV1Api, cfg: Config) -> str:
    """Get a token for the service account.

    Default (cfg.prefer_long_lived_token=True): use the SA's long-lived token
    Secret first — this matches ArgoCD's documented external-cluster
    registration model, where the registration is persistent and a TokenRequest
    token (capped at ~1 year on most clusters) would silently expire.

    Legacy ordering (cfg.prefer_long_lived_token=False): TokenRequest first,
    Secret only as a fallback.

    Raises:
        KubeconfigGeneratorError: if no usable token can be obtained.
        ApiException: if the Kubernetes API is unreachable.
    """
    if cfg.prefer_long_lived_token:
        try:
            return get_token_from_secret(v1, cfg)
        except (KubeconfigGeneratorError, ApiException):
            token = create_token_with_kubectl(cfg)
            if token:
                return token
            raise

    token = create_token_with_kubectl(cfg)
    if token:
        return token
    return get_token_from_secret(v1, cfg)


def generate_kubeconfig(cfg: Config) -> None:
    """Generate kubeconfig file for a service account.

    Raises:
        KubeconfigGeneratorError: if the source kubeconfig is malformed,
            the ServiceAccount cannot be located, or no token is available.
    """
    current_kubeconfig = load_kubeconfig_file(cfg.kubeconfig_path)

    config.load_kube_config(config_file=cfg.kubeconfig_path)

    v1 = client.CoreV1Api()

    current_context_name = current_kubeconfig.get("current-context")
    if not current_context_name:
        raise KubeconfigGeneratorError("No current context found")

    current_context = next(
        (ctx for ctx in current_kubeconfig["contexts"] if ctx["name"] == current_context_name),
        None,
    )
    if not current_context:
        raise KubeconfigGeneratorError(f"Context {current_context_name} not found")

    current_cluster_name = current_context["context"]["cluster"]
    current_cluster = next(
        (cls for cls in current_kubeconfig["clusters"] if cls["name"] == current_cluster_name),
        None,
    )
    if not current_cluster:
        raise KubeconfigGeneratorError(f"Cluster {current_cluster_name} not found")

    if cfg.cluster_name == "auto":
        cfg.cluster_name = current_cluster_name

    if cfg.api_server == "auto":
        cfg.api_server = current_cluster["cluster"]["server"]

    try:
        v1.read_namespaced_service_account(cfg.service_account_name, cfg.namespace)
    except ApiException as e:
        raise KubeconfigGeneratorError(
            f"Failed to get ServiceAccount {cfg.service_account_name} in namespace {cfg.namespace}: {e}"
        ) from e

    token = get_service_account_token(v1, cfg)

    new_cluster: dict[str, Any] = {
        "server": cfg.api_server,
    }

    cluster_data = current_cluster["cluster"]
    if "certificate-authority-data" in cluster_data:
        new_cluster["certificate-authority-data"] = cluster_data["certificate-authority-data"]
    elif "certificate-authority" in cluster_data:
        ca_path = Path(cluster_data["certificate-authority"])
        try:
            ca_data = ca_path.read_bytes()
            new_cluster["certificate-authority-data"] = base64.b64encode(ca_data).decode("utf-8")
        except OSError as e:
            logger.warning("Failed to read CA certificate at %s: %s", ca_path, e)
            logger.warning("Setting insecure-skip-tls-verify: true")
            new_cluster["insecure-skip-tls-verify"] = True
    else:
        logger.warning("No CA certificate data found. Setting insecure-skip-tls-verify: true")
        new_cluster["insecure-skip-tls-verify"] = True

    new_kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": cfg.cluster_name,
                "cluster": new_cluster,
            }
        ],
        "users": [
            {
                "name": cfg.service_account_name,
                "user": {"token": token},
            }
        ],
        "contexts": [
            {
                "name": cfg.context_name,
                "context": {
                    "cluster": cfg.cluster_name,
                    "user": cfg.service_account_name,
                    "namespace": cfg.namespace,
                },
            }
        ],
        "current-context": cfg.context_name,
    }

    save_kubeconfig_file(new_kubeconfig, cfg.output_path)


def main() -> None:
    """Main entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cfg = tyro.cli(Config)

    if cfg.output_path == "auto":
        cfg.output_path = f"kubeconfig-{cfg.service_account_name}"
    if cfg.context_name == "auto":
        cfg.context_name = f"{cfg.service_account_name}-context"

    try:
        generate_kubeconfig(cfg)
    except (KubeconfigGeneratorError, ApiException, OSError) as e:
        logger.error("Error generating kubeconfig: %s", e)
        sys.exit(1)

    abs_path = Path(cfg.output_path).resolve()
    print(f"Kubeconfig file created at: {abs_path}")
    print(f"Use with: export KUBECONFIG={abs_path}")


if __name__ == "__main__":
    main()
