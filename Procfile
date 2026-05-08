web: PYTHONPATH=src uvicorn nodalpulse.api.app:app --host 0.0.0.0 --port $PORT
worker: PYTHONPATH=src python -m nodalpulse.worker
scheduler: PYTHONPATH=src python -m nodalpulse.cron
