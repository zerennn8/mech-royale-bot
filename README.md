# survivor-py: Mech Royale bot

Python (standard library only) bot for the Builders Mech Royale track (`mechfight/1`).

- `bot.py`: decision logic. Zone discipline, low-exposure hiding spots inside the next circle, careful looting (cubes, weapons, crates, drones, uncontested supply drops), fighting from cover with lead aiming, retreating to regenerate.
- `world.py`: map model (2 m building raster, terrain, line of sight, 4 m nav grid, exposure).
- `nav.bin`: nav data pre-baked from the kit map (rebuilt at runtime if the map ever changes).
- `mechfight.py`: the kit's protocol wrapper, unchanged.
