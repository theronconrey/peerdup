"""
Custom build step: generate gRPC/protobuf stubs before installing.

Runs automatically during `pip install -e .` or `pip install .`.
Stubs are written to daemon/ (alongside the package code) and are
gitignored — they are always regenerated from proto/ at install time.
"""

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
import os


class build_py(_build_py):
    def run(self):
        _generate_stubs()
        super().run()


def _generate_stubs():
    try:
        from grpc_tools import protoc
        import grpc_tools
    except ImportError:
        raise SystemExit(
            "grpcio-tools is required to build peerdup-daemon.\n"
            "  pip install grpcio-tools"
        )

    base_dir   = os.path.dirname(os.path.abspath(__file__))
    proto_dir  = os.path.join(base_dir, "proto")
    out_dir    = os.path.join(base_dir, "daemon")
    well_known = os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")

    rc = protoc.main([
        "grpc_tools.protoc",
        f"-I{proto_dir}",
        f"-I{well_known}",
        f"--python_out={out_dir}",
        f"--grpc_python_out={out_dir}",
        os.path.join(proto_dir, "registry.proto"),
        os.path.join(proto_dir, "control.proto"),
    ])
    if rc != 0:
        raise SystemExit("protoc failed — check proto/")

    # Fix self-referential bare imports in generated grpc stubs so they
    # resolve correctly from within the `daemon` package.
    _patch(
        os.path.join(out_dir, "registry_pb2_grpc.py"),
        "import registry_pb2 as registry__pb2",
        "from daemon import registry_pb2 as registry__pb2",
    )
    _patch(
        os.path.join(out_dir, "control_pb2_grpc.py"),
        "import control_pb2 as control__pb2",
        "from daemon import control_pb2 as control__pb2",
    )


def _patch(path: str, old: str, new: str):
    with open(path) as f:
        content = f.read()
    with open(path, "w") as f:
        f.write(content.replace(old, new))


setup(cmdclass={"build_py": build_py})
