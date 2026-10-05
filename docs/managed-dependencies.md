# Managed dependencies in Mercury

`mercury pm` manages pinned tools and application dependency generations. Use
`mercury-nightly pm` for the nightly installation. Each command uses that
installation's home and source tree; it does not replace the other channel's
launcher.

```bash
mercury pm --help
mercury pm install uv
mercury pm install chromium
mercury pm install venv
mercury pm doctor
```

Installing `venv` prepares Mercury's current application dependencies and the
dependencies declared by enabled plugins across its profiles. It builds a new
generation and publishes it only after resolution and validation succeed.
Failure retains the previously selected generation and plugin configuration.
The profile's models and each engine's permission settings stay in Mercury's
existing configuration.

The manager runs in a separate locked Python environment. Mercury's application
uses the supported Python provided by its installer; it does not inherit the
manager's Python version. An explicit `MERCURY_PYTHON` override must satisfy the
application's Python requirement.

A newly published generation applies to the next launch. Running agents keep
their current dependency environment. To load changed dependencies in the
Observatory, restart the Observatory; for a CLI session, start a new process.
When an update changes the project's dependency inputs, the launcher uses the
installer runtime until `mercury pm install venv` refreshes the generation.

`mercury pm repair` rebuilds the recorded dependency graph after damage. It
preserves the recorded graph rather than resolving new project requirements.
`mercury pm doctor` reports both installed-state problems and missing tools;
a deliberately partial tool installation can therefore report missing tools.

Automatic dependency installation follows `hermes.security.allow_lazy_installs`
in Mercury's configuration. A named profile can have its own setting. Explicit
`pm install` commands remain deliberate installation requests. This policy is
separate from Hermes/OMP tool approval modes.

Catalog plugin installation retains the reviewed source revision and runs
Mercury's existing security scanner. A successful install is not proof that
every open chat has loaded its tools. If a plugin declares Python dependencies,
prepare them with `pm install venv` before restarting its engine. A failed
credential save can leave downloaded plugin files installed; the card reports
the failure so setup can be completed explicitly.

Maintain source changes and releases using [CONTRIBUTING.md](../CONTRIBUTING.md).
Mercury's regular installer and `mercury update` remain its distribution paths.
