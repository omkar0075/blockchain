# Synthetic demo data only. Never put real patient information here.
INSTITUTIONS = [
    {
        "id": "hospital-a",
        "name": "City Hospital A",
        "type": "hospital",
        "resources": {"diagnostic_slot": 0},
    },
    {
        "id": "hospital-b",
        "name": "Community Hospital B",
        "type": "hospital",
        "resources": {"diagnostic_slot": 4},
    },
    {
        "id": "lab-c",
        "name": "Central Diagnostic Lab C",
        "type": "diagnostic_lab",
        "resources": {"diagnostic_slot": 12},
    },
]

# In-memory demo ledger only; replaced by a real contract integration later.
COMMITMENTS: dict[str, dict] = {}
PROPOSALS: dict[str, dict] = {}
