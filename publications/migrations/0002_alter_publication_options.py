# Generated by Django 4.0.5 on 2023-11-06 23:11

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('publications', '0001_initial'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='publication',
            options={'ordering': ['-id']},
        ),
    ]