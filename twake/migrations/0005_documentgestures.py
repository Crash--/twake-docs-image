import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0035_mention"),
        ("twake", "0004_grantmember"),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentGestures",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("title_at", models.DateTimeField(blank=True, null=True)),
                ("trash_at", models.DateTimeField(blank=True, null=True)),
                ("moved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "copied_from",
                    models.CharField(
                        blank=True, max_length=255, null=True, unique=True
                    ),
                ),
                (
                    "document",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="twake_gestures",
                        to="core.document",
                    ),
                ),
            ],
            options={"db_table": "twake_document_gestures"},
        ),
    ]
