"""
Kinetic DevOps - Interactive CLI for Epicor Kinetic environment management.

This is the host platform for Kinetic development. Users extend it with project submodules.

🚀 QUICK START:
    Windows PowerShell:
        .\scripts\env_init.ps1
    
    Linux/macOS:
        python scripts/env_init.py

📚 THEN USE:
    from kinetic_devops import KineticConfigManager, KineticBaseClient
    from kinetic_devops import KineticBAQService, KineticBOReaderService, KineticFileService
"""

def show_menu():
    """Display interactive menu and examples."""
    print("""
╔════════════════════════════════════════════════════════════════╗
║         KINETIC DEVOPS - Getting Started                       ║
╚════════════════════════════════════════════════════════════════╝

🔧 SETUP (First Time):
   PowerShell:  .\\scripts\\env_init.ps1
   Python:      python scripts/env_init.py
   Then:        python -m kinetic_devops.auth store

✅ VALIDATION:
   python scripts/validate.py               # Check environment setup
   python -m kinetic_devops.auth validate   # Test Kinetic connection

📜 HELPER SCRIPTS:
   python scripts/pull_tax_configs.py       # Export tax configurations
   python scripts/refresh_post_db.py        # Post-DB refresh operations
   python test_services.py                  # Test service imports

📖 DOCUMENTATION:
   Documents/ARCHITECTURE.md                # Design & examples
   scripts/README.md                        # Helper script reference

🐍 IN YOUR CODE:
   from kinetic_devops import KineticConfigManager, KineticBaseClient
   from kinetic_devops import KineticBAQService, KineticBOReaderService

📌 TIPS:
   • Use 'python -m kinetic_devops.auth store' to add new environments
   • Projects are Git submodules inside this core repository
   • See Documents/ARCHITECTURE.md for comprehensive examples

    """)


def main():
    show_menu()


if __name__ == "__main__":
    main()
