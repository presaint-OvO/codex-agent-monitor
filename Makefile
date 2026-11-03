.PHONY: test install uninstall

test:
	python3 -m py_compile src/monitor.py src/hook.py scripts/configure_hooks.py tests/test_monitor.py tests/test_configure_hooks.py
	python3 -m unittest discover -s tests -p 'test_*.py'

install:
	./install.sh

uninstall:
	./uninstall.sh
