PROTO_DIR = proto
PROTO_OUT = .

.PHONY: install proto test clean

## Install all dependencies
install:
	pip install -e ".[dev]"

## Generate gRPC stubs for the control proto only.
## Registry stubs are re-exported from the registry package (daemon/daemon/registry_pb2*.py)
## to avoid duplicate descriptor-pool registrations when both packages share a process.
proto:
	python -m grpc_tools.protoc \
		-I $(PROTO_DIR) \
		-I "$$(python -c 'import grpc_tools; import os; print(os.path.dirname(grpc_tools.__file__))')"/_proto \
		--python_out=$(PROTO_OUT) \
		--grpc_python_out=$(PROTO_OUT) \
		$(PROTO_DIR)/control.proto
	@echo "Stubs written: control_pb2*.py"
	@echo "Registry stubs: re-exported from registry package (no regeneration needed)"

## Run tests
test:
	pytest -v

## Watch a directory for changes and show events (useful for testing watcher)
watch-test:
	@python -c "import time, asyncio; from daemon.watcher.fs import FileWatcher; q = asyncio.Queue(); loop = asyncio.new_event_loop(); bridge = FileWatcher.make_async_bridge(loop, q); w = FileWatcher(on_change=bridge); w.start(); w.add_share('test', '/tmp/peerdup-test'); print('Watching /tmp/peerdup-test (Ctrl-C to stop)'); import signal; signal.signal(signal.SIGINT, lambda *_: (w.stop(), exit(0))); time.sleep(3600)"

clean:
	rm -f control_pb2.py control_pb2_grpc.py
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
