PART ?= ring
WS   ?= $(CURDIR)/out
SCAD ?= models/spirograph.scad

install:
	pip install -e ".[verify]"

verify:
	cadloop-verify

echo:
	OPENSCAD_WORKSPACE=$(CURDIR) python -c "import sys;sys.exit(0)"
	openscad -o /tmp/x.echo $(SCAD)
	@grep ECHO /tmp/x.echo || true

render:
	@mkdir -p $(WS)
	openscad -o $(WS)/$(PART).stl -D 'part="$(PART)"' $(SCAD)

smoke:
	python tests/smoke.py

all: verify smoke

.PHONY: install verify echo render smoke all
