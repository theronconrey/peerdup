# Re-export from the registry package so that both packages share a single
# descriptor pool registration for registry.proto.
from registry.registry_pb2_grpc import *    # noqa: F401, F403
from registry.registry_pb2_grpc import (   # noqa: F401
    RegistryServiceStub,
    RegistryServiceServicer,
    add_RegistryServiceServicer_to_server,
    RegistryService,
)
