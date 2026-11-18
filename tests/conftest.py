import os

# Тесты не должны трогать настоящую станцию и базу: AMI выключен, порт базы заведомо пустой.
os.environ["TELEPHONY_AMI_ENABLED"] = "false"
os.environ["TELEPHONY_DB_HOST"] = "127.0.0.1"
os.environ["TELEPHONY_DB_PORT"] = "1"
