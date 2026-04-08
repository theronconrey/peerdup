FROM python:3.12-slim

WORKDIR /app

# Install build deps for grpcio (needs C compiler)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir -e ".[dev]"

# Generate gRPC stubs
RUN python -m grpc_tools.protoc \
    -I proto \
    -I "$(python -c 'import grpc_tools, os; print(os.path.dirname(grpc_tools.__file__))')/_proto" \
    --python_out=. \
    --grpc_python_out=. \
    proto/registry.proto

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 50051

ENTRYPOINT ["/entrypoint.sh"]
