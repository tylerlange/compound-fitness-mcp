"""Source-agnostic training-data store behind the fitness coach agent.

The core package (db, canonical, adapters, analytics) imports nothing from the
agent runtime or any chat UI and is fully unit-testable offline. Only
``agent/`` couples to the model and the MCP transport.
"""

__version__ = "0.1.0"
