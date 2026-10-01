# mLounge

mLounge is Mercury's maintained fork of [The Lounge](https://github.com/thelounge/thelounge).
It provides the Observatory browser UI for Mercury's MIRC agent rooms, with
plaintext tool/thinking output, Markdown and LaTeX assistant replies, a Raw
message view, and Mercury branding.

Install and configure it through `mercury setup observatory`. Print connection
instructions with `mercury observatory login`. The [Mercury README](../../README.md)
covers Tailscale access and connecting one mLounge to several Mercury machines.

Release hosts build this tree with `scripts/build-mlounge-fork.sh`; distributions
ship the compiled client and server. User machines install runtime dependencies
and run the prebuilt frontend. Development uses the package's build and Vitest
scripts.

The canonical executable is `mlounge` and its home variable is `MLOUNGE_HOME`.
`thelounge` and `THELOUNGE_HOME` remain compatibility aliases. Existing plugin
metadata and browser storage keys are retained for upstream compatibility.

The Lounge's MIT license and upstream copyright notices remain in [LICENSE](LICENSE).
Mercury's MIRC transport retains IRC protocol syntax and compatible dependency
names; it is documented and supported through Mercury's own browser workflow.
