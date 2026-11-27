import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("twake", "0003_grant_group_drive_members_as_of"),
    ]

    operations = [
        migrations.CreateModel(
            name="GrantMember",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("email", models.CharField(max_length=255)),
                ("sub", models.CharField(blank=True, max_length=255)),
                ("workplace_fqdn", models.CharField(blank=True, max_length=255)),
                ("read_only", models.BooleanField(default=False)),
                ("at", models.DateTimeField()),
                ("removed", models.BooleanField(default=False)),
                (
                    "grant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="members",
                        to="twake.grant",
                    ),
                ),
            ],
            options={
                "db_table": "twake_grant_member",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("grant", "email"),
                        name="twake_grant_member_unique_email",
                    )
                ],
            },
        ),
    ]
