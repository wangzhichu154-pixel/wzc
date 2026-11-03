from django.core.management.base import BaseCommand

from accounts.tasks import drain_email_outbox


class Command(BaseCommand):
    help = "Deliver a bounded batch of pending account mail from the durable outbox."

    def handle(self, *args, **options):
        drain_email_outbox()
        self.stdout.write("Processed eligible email jobs; inspect EmailJob status for delivery evidence.")
