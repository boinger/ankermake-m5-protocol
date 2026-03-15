import sqlite3

import pytest

from web.service.filament import FilamentStore, _DEFAULTS, _sanitize_text


def test_sanitize_text_strips_html_tags():
    assert _sanitize_text("<b>PLA</b><script>alert(1)</script>") == "PLAalert(1)"
    assert _sanitize_text(42) == 42


def test_filament_store_seeds_defaults(tmp_path):
    store = FilamentStore(tmp_path / "filaments.db")

    profiles = store.list_all()

    assert len(profiles) == len(_DEFAULTS)
    assert profiles[0]["name"] == "Generic PLA"


def test_filament_store_create_update_duplicate_and_delete(tmp_path):
    store = FilamentStore(tmp_path / "filaments.db")

    created = store.create({
        "name": "My <b>PLA</b>",
        "brand": "<script>Bad</script>Brand",
        "material": "PLA",
        "notes": "safe <i>note</i>",
        "nozzle_temp_other_layer": 215,
    })

    assert created["name"] == "My PLA"
    assert created["brand"] == "BadBrand"
    assert created["notes"] == "safe note"

    updated = store.update(created["id"], {"notes": "<b>updated</b>"})
    duplicate = store.duplicate(created["id"])

    assert updated["notes"] == "updated"
    assert duplicate["name"] == "My PLA (copy)"

    assert store.delete(created["id"]) is True
    assert store.get(created["id"]) is None
    assert store.delete(999999) is False


def test_filament_store_requires_name(tmp_path):
    store = FilamentStore(tmp_path / "filaments.db")

    with pytest.raises(ValueError, match="name is required"):
        store.create({"material": "PLA"})


def test_filament_store_migrates_legacy_columns(tmp_path):
    db_path = tmp_path / "legacy_filaments.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE filaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            brand TEXT DEFAULT '',
            material TEXT DEFAULT 'PLA',
            color TEXT DEFAULT '#FFFFFF',
            nozzle_temp INTEGER DEFAULT 220,
            bed_temp INTEGER DEFAULT 60,
            filament_diameter REAL DEFAULT 1.75,
            pressure_advance REAL DEFAULT 0.0,
            max_volumetric_speed REAL DEFAULT 15.0,
            travel_speed INTEGER DEFAULT 120,
            perimeter_speed INTEGER DEFAULT 60,
            infill_speed INTEGER DEFAULT 80,
            cooling_enabled INTEGER DEFAULT 1,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        INSERT INTO filaments (
            name, material, color, nozzle_temp, bed_temp,
            filament_diameter, pressure_advance, max_volumetric_speed,
            travel_speed, perimeter_speed, infill_speed, cooling_enabled, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("Legacy PLA", "PLA", "#FFFFFF", 222, 61, 1.75, 0.1, 12.0, 120, 60, 80, 1, ""),
    )
    conn.commit()
    conn.close()

    store = FilamentStore(db_path)
    legacy = store.list_all()[0]

    assert legacy["name"] == "Legacy PLA"
    assert legacy["nozzle_temp_other_layer"] == 222
    assert legacy["bed_temp_other_layer"] == 61
    assert "nozzle_temp_first_layer" in legacy
    assert "wipe_speed" in legacy
