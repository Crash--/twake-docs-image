from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("twake", "0002_workplaceuser"),
    ]

    operations = [
        migrations.AddField(
            model_name="grant",
            name="organization_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="grant",
            name="external_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="grant",
            name="owner_instance",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="grant",
            name="members_as_of",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
