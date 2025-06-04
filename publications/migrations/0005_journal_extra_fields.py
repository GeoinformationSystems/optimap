from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('publications', '0004_journal_alter_publication_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='journal',
            name='publisher_name',
            field=models.CharField(
                max_length=255,
                null=True,
                blank=True,
                help_text='Name of the publisher as returned by OpenAlex'
            ),
        ),
        migrations.AddField(
            model_name='journal',
            name='works_count',
            field=models.IntegerField(
                null=True,
                blank=True,
                help_text='Total number of works (articles, books, etc.) from this journal'
            ),
        ),
        migrations.AddField(
            model_name='journal',
            name='works_api_url',
            field=models.URLField(
                max_length=512,
                null=True,
                blank=True,
                help_text='API endpoint to list all works from this journal'
            ),
        ),
        migrations.AddField(
            model_name='journal',
            name='openalex_url',
            field=models.URLField(
                max_length=512,
                null=True,
                blank=True,
                help_text='Canonical OpenAlex URL for this journal (source.id)'
            ),
        ),
    ]
