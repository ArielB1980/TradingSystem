web: python -m src.health
worker: python run.py live --force
dashboard: streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
