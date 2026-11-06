from django.apps import AppConfig


class SafeDeleteConfig(AppConfig):

    name = 'safedelete_pro'
    verbose_name = 'Safe Delete Pro'

    def ready(self):
        pass
