from django.core.management.base import BaseCommand

from accounts.tasks import prune_account_history


class Command(BaseCommand):
    help = "Prune a bounded batch of expired account records according to configured retention."

    def handle(self, *args, **options):
        prune_account_history()
        self.stdout.write("Retention batch complete; inspect account_retention_pruned log for counts.")
