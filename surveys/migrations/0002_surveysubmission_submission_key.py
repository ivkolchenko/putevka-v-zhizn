from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('surveys', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='surveysubmission',
            name='submission_key',
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
    ]
