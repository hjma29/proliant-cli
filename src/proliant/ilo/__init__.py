"""
proliant ilo — HPE iLO Redfish management toolkit.

Package layout
--------------
config.py    – constants, HOSTS_FILE path, load_hosts()
client.py    – ilo_session() context manager, shared URI navigators
inventory.py – read-only fetch functions (firmware, NIC, storage, CPU, memory)
power.py     – server power operations  (reset, power-on/off)
firmware.py  – firmware upload / flash operations
cli.py       – argparse wiring, parallel dispatch, table printing, main()

Entry point
-----------
examples/Redfish/hj_software_firmware_inventory.py  (thin wrapper → cli.main)
"""

__version__ = "0.1.0"
