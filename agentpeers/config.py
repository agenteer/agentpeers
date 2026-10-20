"""Environment settings shared by the CLI and native adapters."""
import os


def setting(name, default=None):
    """Prefer the current name; accept the former name during migration."""
    return os.environ.get("AGENTPEERS_" + name,
                          os.environ.get("PEERBRIDGE_" + name, default))
