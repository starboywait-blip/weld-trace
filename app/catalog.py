"""Pick-lists (item types, materials, processes, electrodes) and advisory compatibility checks.

The checks are guidance only. They never block saving a joint, because the welder or inspector
knows the real situation. Free-text entries are stored as "Other: <text>".
"""

ITEM_TYPES = [
    ("tank", "Tank"),
    ("frame", "Frame"),
    ("gate", "Gate"),
    ("chair", "Chair / stool"),
    ("table", "Table / bench"),
    ("stairs", "Stairs / ladder"),
    ("handrail", "Handrail / balustrade"),
    ("grille", "Window grille / burglar bars"),
    ("door", "Door / door frame"),
    ("rack", "Shelf / rack"),
    ("other", "Other"),
]

MATERIALS = [
    ("mild_steel", "Mild steel (plate, flat bar, angle)"),
    ("mild_tube", "Mild steel tube / pipe (square, round)"),
    ("rebar", "Ribbed steel bar (rebar)"),
    ("galvanized", "Galvanized steel"),
    ("stainless_304", "Stainless steel 304"),
    ("stainless_316", "Stainless steel 316"),
    ("aluminium", "Aluminium"),
    ("cast_iron", "Cast iron"),
    ("high_carbon", "Spring / high-carbon steel"),
    ("other", "Other (type it in)"),
]

PROCESSES = ["Stick", "MIG", "TIG", "Flux-core"]

ELECTRODES = {
    "Stick": [
        ("E6013", "E6013: general purpose, easy to use on clean mild steel"),
        ("E6011", "E6011: deeper penetration, rusty or dirty steel"),
        ("E7018", "E7018: low-hydrogen, structural work and rebar"),
        ("E308L-16", "E308L-16: stainless 304"),
        ("E316L-16", "E316L-16: stainless 316"),
        ("E309L-16", "E309L-16: stainless to mild steel"),
        ("ENi-CI", "ENi-CI: cast iron (nickel)"),
        ("other", "Other (type it in)"),
    ],
    "MIG": [
        ("ER70S-6", "ER70S-6 wire: mild steel"),
        ("ER308LSi", "ER308LSi wire: stainless 304"),
        ("ER316LSi", "ER316LSi wire: stainless 316"),
        ("ER4043", "ER4043 wire: aluminium"),
        ("ER5356", "ER5356 wire: aluminium"),
        ("other", "Other (type it in)"),
    ],
    "TIG": [
        ("ER70S-2", "ER70S-2 rod: mild steel"),
        ("ER308L", "ER308L rod: stainless 304"),
        ("ER316L", "ER316L rod: stainless 316"),
        ("ER4043", "ER4043 rod: aluminium"),
        ("ER5356", "ER5356 rod: aluminium"),
        ("other", "Other (type it in)"),
    ],
    "Flux-core": [
        ("E71T-1", "E71T-1: gas-shielded, mild steel"),
        ("E71T-11", "E71T-11: self-shielded (no gas), mild steel"),
        ("E308LT-1", "E308LT-1: stainless 304"),
        ("other", "Other (type it in)"),
    ],
}

ITEM_KEYS = {k for k, _ in ITEM_TYPES}
MATERIAL_KEYS = {k for k, _ in MATERIALS if k != "other"}


def options() -> dict:
    row = lambda pairs: [{"value": k, "label": v} for k, v in pairs]
    return {
        "item_types": row(ITEM_TYPES),
        "materials": row(MATERIALS),
        "processes": PROCESSES,
        "electrodes": {p: row(v) for p, v in ELECTRODES.items()},
    }


def material_label(value: str) -> str:
    return dict(MATERIALS).get(value, value or "")


def item_label(value: str) -> str:
    return dict(ITEM_TYPES).get(value, value or "")


def check_compat(material: str, process: str, electrode: str) -> list[str]:
    """Return advisory notes about the material / process / electrode combination."""
    notes: list[str] = []
    if material not in MATERIAL_KEYS or electrode.startswith("Other:"):
        return notes  # free-text entries: we can't judge

    e = electrode.upper()
    stainless_filler = e.startswith(("E30", "E31", "ER30", "ER31"))
    is_309 = e.startswith(("E309", "ER309"))
    alu_filler = e.startswith(("ER40", "ER53"))
    nickel_filler = e.startswith("ENI")
    steel = material in {"mild_steel", "mild_tube", "rebar", "galvanized", "high_carbon"}

    known = {k for k, _ in ELECTRODES.get(process, [])}
    if known and electrode not in known:
        notes.append(f"{electrode} is not normally used with the {process} process. Check the process and electrode.")

    if material.startswith("stainless"):
        if not stainless_filler:
            notes.append("Stainless steel needs a stainless filler (308L for 304, 316L for 316). A mild steel filler will rust and may crack.")
        elif material == "stainless_316" and e.startswith(("E308", "ER308")):
            notes.append("A 308 filler on 316 stainless loses some corrosion resistance. Use 316L.")
    elif material == "aluminium":
        if not alu_filler or process in ("Stick", "Flux-core"):
            notes.append("Aluminium needs ER4043 or ER5356 filler with MIG or TIG and argon gas. Stick and steel fillers do not work.")
    elif material == "cast_iron":
        if not nickel_filler:
            notes.append("Cast iron needs a nickel electrode (ENi-CI), preheat and slow cooling. Steel electrodes usually crack.")
    elif steel:
        if stainless_filler and not is_309:
            notes.append("Stainless filler on plain steel is wasteful and can crack. Use E6013, E7018 or ER70S-6. For steel joined to stainless use E309L.")
        if alu_filler or nickel_filler:
            notes.append("This filler is not suitable for steel.")

    if material == "rebar":
        if e.startswith("E60"):
            notes.append("Welding reinforcing bar normally specifies low-hydrogen E7018. E6013 and E6011 can crack on it. Also confirm the bar is a weldable grade.")
        else:
            notes.append("Rebar carbon content varies. Confirm it is a weldable grade, keep low-hydrogen rods dry, and preheat in thick or cold conditions.")
    if material == "high_carbon" and e != "E7018":
        notes.append("High-carbon or spring steel cracks easily. Use low-hydrogen E7018, preheat and cool slowly.")
    if material == "galvanized":
        notes.append("Galvanized steel gives off harmful zinc fumes. Grind the coating off the joint and ventilate well.")
    return notes
