#!/usr/bin/env python3
"""
Entry point for the Kraken Futures SMC Trading System.
Wraps src/cli.py to ensure correct import resolution.
"""
import sys
import os

# Ensure project root is in python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.cli import app

if __name__ == "__main__":
    app()
