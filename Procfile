web: python -m src.health
worker: python migrate_schema.py && python -m src.entrypoints.prod_live
dashboard: streamlit run src/dashboard/streamlit_app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true --server.baseUrlPath /dashboard
