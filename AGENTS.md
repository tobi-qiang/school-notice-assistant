# Project agent conventions

- Read `README.md` before implementation.
- Keep all project files inside this directory.
- Use UTF-8 without BOM for source and data files.
- Do not edit files outside the paths assigned in the delegation task.
- Preserve the public dataclasses and enums in `app/models.py`; coordinate additions rather than changing existing fields.
- Network and AI failures must degrade safely and must not corrupt committed notice data.
- Tests must not call live school or AI endpoints; use fixtures and fake transports.
- Never log API keys or full sensitive notice bodies.
