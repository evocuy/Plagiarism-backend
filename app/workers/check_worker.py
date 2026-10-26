import logging

from app.services.check_queue_worker import CheckQueueWorker


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    CheckQueueWorker().run_forever()
