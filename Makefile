# ==============================================================================
# Makefile - dhanytv Automation
# ==============================================================================

PYTHON      ?= python3
SCRIPT_DIR  := update-script

# Default files
SOURCE      ?= dhanytv.m3u
OUTPUT      ?= favorites.m3u
CONFIG      ?= $(SCRIPT_DIR)/favorites.json
EPG_IN      ?= epg.xml
EPG_OUT     ?= favorites-epg.xml

.PHONY: all help favorites fav epg validate-keys check

# Default target menampilkan panduan
.DEFAULT_GOAL := help

help:
	@echo "=========================================================="
	@echo "  dhanytv - Helper Makefile"
	@echo "=========================================================="
	@echo "Perintah yang tersedia:"
	@echo "  make favorites        - Generate $(OUTPUT) dari $(SOURCE)"
	@echo "  make epg              - Generate $(EPG_OUT) sesuai daftar favorites"
	@echo "  make all              - Jalankan make favorites lalu make epg"
	@echo "  make validate-keys    - Cek validasi ClearKey pada playlist"
	@echo ""
	@echo "Kustomisasi Parameter (opsional):"
	@echo "  make favorites SOURCE=dhanytv.m3u OUTPUT=favorites.m3u"
	@echo "  make favorites CONFIG=update-script/favorites.json"
	@echo "=========================================================="

favorites:
	@echo "==> Menjalankan generate_favorites.py..."
	$(PYTHON) $(SCRIPT_DIR)/generate_favorites.py -s $(SOURCE) -o $(OUTPUT) -c $(CONFIG)

fav: favorites

epg:
	@echo "==> Menjalankan fix_epg.py..."
	$(PYTHON) $(SCRIPT_DIR)/fix_epg.py $(EPG_IN) $(EPG_OUT) -f $(OUTPUT) -c $(CONFIG)

all: favorites epg

validate-keys:
	@echo "==> Menjalankan validate_clearkeys.py..."
	$(PYTHON) $(SCRIPT_DIR)/validate_clearkeys.py $(SOURCE)

check:
	@echo "==> Memeriksa sintaks skrip python..."
	$(PYTHON) -m py_compile $(SCRIPT_DIR)/generate_favorites.py
	$(PYTHON) -m py_compile $(SCRIPT_DIR)/fix_epg.py
	@echo "OK."
