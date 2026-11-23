# Target Execution Profiles

Version-controlled target execution profiles live here. They describe candidate
mobility-device and SDI execution environments without claiming those
environments are available or validated.

The six S-04 profiles preserve the accepted order: Waffle native ARM64, Waffle
Xycar AMD64, Burger native ARM64, Burger Xycar AMD64, Waffle Jetson ARM64, and
Burger Jetson ARM64.

Each profile's `provenance` says whether an accountable source declared its
facts. All six are placeholders: their hardware and software values are
assumed, not sourced or measured, so CI may check against these values but
never counts them as evidence, and the composition outputs carry the label.
Each host names one physical machine. In the Xycar profiles the AMD64 Xycar
stands in for an AMD64 TurtleBot, and no fixed service split is supplied, so CI
places services within the declared devices and connections. Their notes carry
the operator's intent that the robot runs only the services that need it; no
check enforces it.

The first Fixture inputs named a Jetson Nano edge platform in both Jetson
profiles. The agreed edge target is the Jetson Orin Nano: the Waffle profile
was corrected earlier, and the Burger profile's `jetson-nano` host is now
`orin`, matching it.
