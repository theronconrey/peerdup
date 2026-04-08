PROTO_DIR = proto
PROTO_OUT = .

.PHONY: install proto test clean

## Install all dependencies
install:
	pip install -e ".[dev]"

## Generate gRPC stubs for both proto files
proto:
	python -m grpc_tools.protoc \
		-I $(PROTO_DIR) \
		-I "$$(python -c 'import grpc_tools; import os; print(os.path.dirname(grpc_tools.__file__))')"/_proto \
		--python_out=$(PROTO_OUT) \
		--grpc_python_out=$(PROTO_OUT) \
		$(PROTO_DIR)/registry.proto \
		$(PROTO_DIR)/control.proto
	@echo "Stubs written: registry_pb2*.py  control_pb2*.py"

## Run tests
test:
	pytest -v

## Watch a directory for changes and show events (useful for testing watcher)
watch-test:
	@python -c "
import time, asyncio
from daemon.watcher.fs import FileWatcher, FileChangedEvent
q = asyncio.Queue()
loop = asyncio.new_event_loop()
bridge = FileWatcher.make_async_bridge(loop, q)
watcher = FileWatcher(on_change=bridge)
watcher.start()
watcher.add_share('test-share', '/tmp/peerdup-test')
print('Watching /tmp/peerdup-test — create/modify/delete files to see events')
try:
    time.sleep(60)
except KeyboardInterrupt:
    pass
watcher.stop()
"

clean:
	rm -f registry_pb2.py registry_pb2_grpc.py
	rm -f control_pb2.py control_pb2_grpc.py
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
