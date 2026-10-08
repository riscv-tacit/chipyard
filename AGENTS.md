# Chipyard development conventions

## Naming

Choose the naming style by what an identifier represents, even when both hardware
and software are expressed in Scala:

- Use `snake_case` for hardware signals, ports, wires, registers, and hardware
  module instances: `pmu_increment`, `pmu_clock`, `tile_pmu`.
- Use `camelCase` for Scala values, variables, methods, metadata, and configuration
  parameters: `registry`, `counterBits`, `incrementBits`, `register`, `build`.
- Keep `PascalCase` for classes, traits, types, companion objects, and configuration
  key objects: `PMURegistry`, `TilePMU`, `PMUScopeKey`.
- A Scala holder of hardware handles remains a software value. For example,
  `tilePMU: PMUAttachment` uses camelCase; hardware fields it exposes use
  snake_case. Compiler targets and serialized metadata are software values too.

Apply these conventions to new code and names changed as part of the task. Keep
unrelated existing code and upstream APIs outside a task's naming cleanup.
