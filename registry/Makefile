PROTO_SRC   = proto/registry.proto
PROTO_OUT   = .   # generated stubs land in repo root alongside registry/

.PHONY: install proto test cert clean

## Install all dependencies (run once after clone)
install:
	pip install -e ".[dev]"

## Generate gRPC stubs from proto definition
## Output: registry_pb2.py  registry_pb2_grpc.py
## The grpc stub uses a bare "import registry_pb2" which only works when
## registry/ is on sys.path.  We fix it to an absolute package import so it
## works when imported as "from registry import registry_pb2_grpc" from any
## Python context (e.g. daemon integration tests).
proto:
	python -m grpc_tools.protoc \
		-I proto \
		-I "$$(python -c 'import grpc_tools; import os; print(os.path.dirname(grpc_tools.__file__))')"/_proto \
		--python_out=$(PROTO_OUT) \
		--grpc_python_out=$(PROTO_OUT) \
		$(PROTO_SRC)
	@sed -i 's/^import registry_pb2 as registry__pb2$$/from registry import registry_pb2 as registry__pb2/' $(PROTO_OUT)/registry_pb2_grpc.py
	@echo "Stubs written to $(PROTO_OUT)/registry_pb2*.py (import path fixed)"

## Run tests (requires: make install)
test:
	pytest -v

## Generate a self-signed TLS cert for development (NOT for production)
## Production: use Let's Encrypt or your own CA.
cert:
	openssl req -x509 -newkey rsa:4096 -keyout server.key -out server.crt \
		-days 365 -nodes \
		-subj "/CN=peerdup-registry-dev"
	@echo "Generated server.key and server.crt (dev only)"

## Generate mTLS client cert signed by the same dev CA
client-cert:
	openssl genrsa -out client.key 4096
	openssl req -new -key client.key -out client.csr \
		-subj "/CN=peerdup-client-dev"
	openssl x509 -req -in client.csr -CA server.crt -CAkey server.key \
		-CAcreateserial -out client.crt -days 365
	@echo "Generated client.key and client.crt (dev only)"

clean:
	rm -f registry_pb2.py registry_pb2_grpc.py
	rm -f server.key server.crt client.key client.crt client.csr server.srl
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
