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
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import tyro
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException


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
    """Token expiry in hours (8760 = 1 year)."""


def load_kubeconfig_file(kubeconfig_path: str) -> dict[str, Any]:
    """Load kubeconfig from file."""
    with open(kubeconfig_path) as f:
        return yaml.safe_load(f)


def save_kubeconfig_file(kubeconfig: dict[str, Any], output_path: str) -> None:
    """Save kubeconfig to file."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, "w") as f:
        yaml.safe_dump(kubeconfig, f, default_flow_style=False)

    # Set file permissions to 0600 (rw-------)
    output.chmod(0o600)


def create_token_with_kubectl(cfg: Config) -> str | None:
    """Try to create a token using kubectl command."""
    try:
        args = ["kubectl", "create", "token", cfg.service_account_name, "-n", cfg.namespace]

        if cfg.kubeconfig_path:
            args.append(f"--kubeconfig={cfg.kubeconfig_path}")

        args.append(f"--duration={cfg.token_expiry_hours}h")

        result = subprocess.run(args, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # This is expected to fail on older Kubernetes versions or if kubectl is not available
        return None


def get_token_from_secret(v1: client.CoreV1Api, cfg: Config) -> str:
    """Get a token from the service account's secret."""
    # Get ServiceAccount to find its secrets
    sa = v1.read_namespaced_service_account(cfg.service_account_name, cfg.namespace)

    # Check if the ServiceAccount has any secrets
    if not sa.secrets:
        raise ValueError("Service account has no secrets")

    # Get the first secret (token secret)
    secret_name = sa.secrets[0].name
    secret = v1.read_namespaced_secret(secret_name, cfg.namespace)

    # Get token from secret
    if "token" not in secret.data:
        raise ValueError(f"Token not found in secret {secret_name}")

    token_data = secret.data["token"]
    # Decode from base64
    return base64.b64decode(token_data).decode("utf-8")


def get_service_account_token(v1: client.CoreV1Api, cfg: Config) -> str:
    """Get a token for the service account."""
    # First, try to use kubectl to create a token (for newer Kubernetes versions)
    token = create_token_with_kubectl(cfg)
    if token:
        return token

    # Fall back to getting a token from a secret (for older Kubernetes versions)
    return get_token_from_secret(v1, cfg)


def generate_kubeconfig(cfg: Config) -> None:
    """Generate kubeconfig file for a service account."""
    # Load the current kubeconfig file
    current_kubeconfig = load_kubeconfig_file(cfg.kubeconfig_path)

    # Load the Kubernetes configuration
    config.load_kube_config(config_file=cfg.kubeconfig_path)

    # Create Kubernetes API client
    v1 = client.CoreV1Api()

    # Get current context and cluster info
    current_context_name = current_kubeconfig.get("current-context")
    if not current_context_name:
        raise ValueError("No current context found")

    current_context = next(
        (ctx for ctx in current_kubeconfig["contexts"] if ctx["name"] == current_context_name),
        None,
    )
    if not current_context:
        raise ValueError(f"Context {current_context_name} not found")

    current_cluster_name = current_context["context"]["cluster"]
    current_cluster = next(
        (cls for cls in current_kubeconfig["clusters"] if cls["name"] == current_cluster_name),
        None,
    )
    if not current_cluster:
        raise ValueError(f"Cluster {current_cluster_name} not found")

    # Set default cluster name if not provided
    if cfg.cluster_name == "auto":
        cfg.cluster_name = current_cluster_name

    # Set default API server if not provided
    if cfg.api_server == "auto":
        cfg.api_server = current_cluster["cluster"]["server"]

    # Verify the ServiceAccount exists
    try:
        v1.read_namespaced_service_account(cfg.service_account_name, cfg.namespace)
    except ApiException as e:
        raise ValueError(
            f"Failed to get ServiceAccount {cfg.service_account_name} in namespace {cfg.namespace}: {e}"
        ) from e

    # Get service account token
    token = get_service_account_token(v1, cfg)

    # Create a new kubeconfig
    new_cluster: dict[str, Any] = {
        "server": cfg.api_server,
    }

    # Add CA certificate data if available
    cluster_data = current_cluster["cluster"]
    if "certificate-authority-data" in cluster_data:
        new_cluster["certificate-authority-data"] = cluster_data["certificate-authority-data"]
    elif "certificate-authority" in cluster_data:
        ca_path = Path(cluster_data["certificate-authority"])
        try:
            ca_data = ca_path.read_bytes()
            new_cluster["certificate-authority-data"] = base64.b64encode(ca_data).decode("utf-8")
        except Exception as e:
            print(f"Warning: Failed to read CA certificate: {e}")
            print("Setting insecure-skip-tls-verify: true")
            new_cluster["insecure-skip-tls-verify"] = True
    else:
        print("Warning: No CA certificate data found. Setting insecure-skip-tls-verify: true")
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

    # Save the kubeconfig to file
    save_kubeconfig_file(new_kubeconfig, cfg.output_path)


def main() -> None:
    """Main entry point."""
    cfg = tyro.cli(Config)

    # Apply defaults for optional fields
    if cfg.output_path == "auto":
        cfg.output_path = f"kubeconfig-{cfg.service_account_name}"
    if cfg.context_name == "auto":
        cfg.context_name = f"{cfg.service_account_name}-context"

    try:
        generate_kubeconfig(cfg)
        abs_path = Path(cfg.output_path).resolve()
        print(f"Kubeconfig file created at: {abs_path}")
        print(f"Use with: export KUBECONFIG={abs_path}")
    except Exception as e:
        print(f"Error generating kubeconfig: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
