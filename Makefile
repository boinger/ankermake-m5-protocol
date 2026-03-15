PYTHON ?= python3

.PHONY: update diff test check install-tools clean

update:
	@transwarp -D specification -I templates/python/ -L templates/lib -O libflagship -u
	@transwarp -D specification -I templates/js/     -L templates/lib -O static      -u

diff:
	@transwarp -D specification -I templates/python/ -L templates/lib -O libflagship -d
	@transwarp -D specification -I templates/js/     -L templates/lib -O static      -d

test:
	@$(PYTHON) -m pytest

check:
	@$(PYTHON) -m compileall ankerctl.py cli libflagship web tests
	@$(PYTHON) -m pytest

install-tools:
	git submodule update --init
	pip install ./transwarp

clean:
	@find -name '*~' -o -name '__pycache__' -print0 | xargs -0 rm -rfv
