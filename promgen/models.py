# Copyright (c) 2017 LINE Corporation
# These sources are released under the terms of the MIT license: see LICENSE

import json
import logging

import django.contrib.sites.models
from django.conf import settings
from django.contrib.contenttypes.fields import (GenericForeignKey,
                                                GenericRelation)
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.forms.models import model_to_dict
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.text import slugify

import promgen.templatetags.promgen as macro
from promgen import plugins, tests, validators
from promgen.shortcuts import resolve_domain

logger = logging.getLogger(__name__)


class Site(django.contrib.sites.models.Site):
    # Proxy model for sites so that we can easily
    # query our related Rules
    rule_set = GenericRelation('Rule')

    def get_absolute_url(self):
        return reverse('rules-list')

    class Meta:
        proxy = True


class DynamicParent(models.Model):
    class Meta:
        abstract = True

    @classmethod
    def create(cls, obj, **kwargs):
        return cls.objects.create(
            object_id=obj.id,
            content_type_id=ContentType.objects.get_for_model(obj).id,
            **kwargs
        )

    @classmethod
    def filter(cls, obj, **kwargs):
        return cls.objects.filter(
            object_id=obj.id,
            content_type_id=ContentType.objects.get_for_model(obj).id,
            **kwargs
        )

    @classmethod
    def get_or_create(cls, **kwargs):
        if 'obj' in kwargs:
            obj = kwargs.pop('obj')
            kwargs['object_id'] = obj.id
            kwargs['content_type_id'] = ContentType.objects.get_for_model(obj).id
        if 'defaults' in kwargs and 'obj' in kwargs['defaults']:
            obj = kwargs['defaults'].pop('obj')
            kwargs['defaults']['object_id'] = obj.id
            kwargs['defaults']['content_type_id'] = ContentType.objects.get_for_model(obj).id

        return cls.objects.get_or_create(**kwargs)


class Sender(DynamicParent):
    sender = models.CharField(max_length=128)
    value = models.CharField(max_length=128)
    alias = models.CharField(max_length=128, blank=True)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, limit_choices_to=(
        models.Q(app_label='auth', model='user') |
        models.Q(app_label='promgen', model='project') | models.Q(app_label='promgen', model='service'))
    )
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True)

    def show_value(self):
        if self.alias:
            return self.alias
        return self.value

    show_value.short_description = 'Value'

    def __str__(self):
        return '{}:{}'.format(self.sender, self.show_value())

    @classmethod
    def driver_set(cls):
        '''Return the list of drivers for Sender model'''
        for entry in plugins.notifications():
            try:
                yield entry.module_name, entry.load()
            except ImportError:
                logger.warning('Error importing %s', entry.module_name)

    __driver = {}
    @property
    def driver(self):
        '''Return configured driver for Sender model instance'''
        if self.sender in self.__driver:
            return self.__driver[self.sender]

        for entry in plugins.notifications():
            try:
                self.__driver[entry.module_name] = entry.load()()
            except ImportError:
                logger.warning('Error importing %s', entry.module_name)
        return self.__driver[self.sender]

    def test(self):
        '''
        Test sender plugin

        Uses the same test json from our unittests but subs in the currently
        tested object as part of the test data
        '''
        data = tests.PromgenTest.data_json('examples', 'alertmanager.json')
        if hasattr(self.content_object, 'name'):
            data['commonLabels'][self.content_type.name] = self.content_object.name
            for alert in data.get('alerts', []):
                alert['labels'][self.content_type.name] = self.content_object.name

        from promgen import tasks
        tasks.send_alert(self.sender, self.value, data)


class Shard(models.Model):
    name = models.CharField(max_length=128, unique=True)
    url = models.URLField(max_length=256)
    proxy = models.BooleanField(default=False,
        help_text='Queries can be proxied to these shards')
    enabled = models.BooleanField(default=True,
        help_text='Able to register new Services and Projects')

    class Meta:
        ordering = ['name']

    def get_absolute_url(self):
        return reverse('shard-detail', kwargs={'pk': self.pk})

    def __str__(self):
        return self.name


class Service(models.Model):
    name = models.CharField(max_length=128, unique=True)
    notifiers = GenericRelation(Sender)
    rule_set = GenericRelation('Rule')
    shard = models.ForeignKey('Shard', on_delete=models.CASCADE)
    description = models.TextField(blank=True)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, default=None)

    class Meta:
        ordering = ['shard', 'name']

    def get_absolute_url(self):
        return reverse('service-detail', kwargs={'pk': self.pk})

    def __str__(self):
        return '{} » {}'.format(self.shard.name, self.name)

    @classmethod
    def default(cls, service_name='Default', shard_name='Default'):
        shard, created = Shard.objects.get_or_create(
            name=shard_name
        )
        if created:
            logger.info('Created default shard')

        service, created = cls.objects.get_or_create(
            name=service_name,
            defaults={'shard': shard}
        )
        if created:
            logger.info('Created default service')
        return service

    @property
    def check_notifiers(self):
        if self.notifiers.count() > 0:
            return True
        for project in self.project_set.all():
            if project.notifiers.count() == 0:
                return False
        return True


class Project(models.Model):
    name = models.CharField(max_length=128, unique=True)
    service = models.ForeignKey('Service', on_delete=models.CASCADE)
    farm = models.ForeignKey('Farm', blank=True, null=True, on_delete=models.SET_NULL)
    notifiers = GenericRelation(Sender)
    rule_set = GenericRelation('Rule')
    description = models.TextField(blank=True)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, default=None)

    class Meta:
        ordering = ['name']

    def get_absolute_url(self):
        return reverse('project-detail', kwargs={'pk': self.pk})

    def __str__(self):
        return '{} » {}'.format(self.service, self.name)


class Farm(models.Model):
    name = models.CharField(max_length=128)
    source = models.CharField(max_length=128)

    class Meta:
        ordering = ['name']
        unique_together = (('name', 'source',),)

    def get_absolute_url(self):
        return reverse('farm-detail', kwargs={'pk': self.pk})

    def refresh(self):
        target = set()
        current = set(host.name for host in self.host_set.all())
        for entry in plugins.discovery():
            if self.source == entry.name:
                target.update(entry.load()().fetch(self.name))

        remove = current - target
        add = target - current

        if add:
            Audit.log('Adding {} to {}'.format(add, self), self)
            Host.objects.bulk_create([
                Host(name=name, farm_id=self.id) for name in add
            ])

        if remove:
            Audit.log('Removing {} from {}'.format(add, self), self)
            Host.objects.filter(farm=self, name__in=remove).delete()

        return add, remove

    @classmethod
    def fetch(cls, source):
        for entry in plugins.discovery():
            if entry.name == source:
                for farm in entry.load()().farms():
                    yield farm

    @cached_property
    def driver(self):
        '''Return configured driver for Farm model instance'''
        for entry in plugins.discovery():
            if entry.name == self.source:
                return entry.load()()

    @property
    def editable(self):
        return not self.driver.remote

    @classmethod
    def driver_set(cls):
        '''Return the list of drivers for Farm model'''
        for entry in plugins.discovery():
            yield entry.name, entry.load()()

    def __str__(self):
        return '{} ({})'.format(self.name, self.source)


class Host(models.Model):
    name = models.CharField(max_length=128)
    farm = models.ForeignKey('Farm', on_delete=models.CASCADE)

    class Meta:
        ordering = ['name']
        unique_together = (('name', 'farm',),)

    def get_absolute_url(self):
        return reverse('host-detail', kwargs={'slug': self.name})

    def __str__(self):
        return '{} [{}]'.format(self.name, self.farm.name)


class Exporter(models.Model):
    job = models.CharField(max_length=128)
    port = models.IntegerField()
    path = models.CharField(max_length=128, blank=True)
    project = models.ForeignKey('Project', on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ['job', 'port']
        unique_together = (('job', 'port', 'project',),)

    def __str__(self):
        return '{}:{}:{} ({})'.format(self.job, self.port, self.path, self.project)

    def get_absolute_url(self):
        return reverse('project-detail', kwargs={'pk': self.project.pk})


class DefaultExporter(models.Model):
    job = models.CharField(max_length=128)
    port = models.IntegerField()
    path = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ['job', 'port']
        unique_together = (('job', 'port', 'path'),)

    def __str__(self):
        return '{}:{}{}'.format(self.job, self.port, self.path or '/metrics')


class URL(models.Model):
    url = models.URLField(max_length=256)
    project = models.ForeignKey('Project', on_delete=models.CASCADE)

    class Meta:
        ordering = ['project__service', 'project', 'url']

    def __str__(self):
        return '{} [{}]'.format(self.project, self.url)


def validate_json_or_empty(value):
    if value == '':
        return
    try:
        json.loads(value)
    except:
        raise ValidationError('Requires json value')


class Rule(DynamicParent):
    name = models.CharField(max_length=128, unique=True, validators=[validators.alphanumeric])
    clause = models.TextField(help_text='Prometheus query')
    duration = models.CharField(
        max_length=128, validators=[validators.prometheusduration],
        help_text="Duration field with postfix. Example 30s, 5m, 1d"
        )
    enabled = models.BooleanField(default=True)
    parent = models.ForeignKey(
        'Rule',
        null=True,
        related_name='overrides',
        on_delete=models.SET_NULL
    )

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, limit_choices_to=(
        models.Q(app_label='sites', model='site') |
        models.Q(app_label='promgen', model='project') |
        models.Q(app_label='promgen', model='service'))
    )
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')
    description = models.TextField(blank=True)

    class Meta:
        ordering = ['content_type', 'object_id', 'name']

    @cached_property
    def labels(self):
        return {obj.name: obj.value for obj in self.rulelabel_set.all()}

    def add_label(self, name, value):
        return RuleLabel.objects.get_or_create(rule=self, name=name, value=value)

    def add_annotation(self, name, value):
        return RuleAnnotation.objects.get_or_create(rule=self, name=name, value=value)

    @cached_property
    def annotations(self):
        _annotations = {obj.name: obj.value for obj in self.ruleannotation_set.all()}
        # Skip when pk is not set, such as when test rendering a rule
        if self.pk and 'rule' not in _annotations:
            _annotations['rule'] = resolve_domain('rule-edit', pk=self.pk)
        return _annotations

    def __str__(self):
        return '{} [{}]'.format(self.name, self.content_object.name)

    def get_absolute_url(self):
        return reverse('rule-edit', kwargs={'pk': self.pk})

    def set_object(self, content_type, object_id):
        self.content_type = ContentType.objects.get(
            model=content_type,
            app_label='promgen'
            )
        self.object_id = object_id

    def copy_to(self, content_type, object_id):
        '''
        Make a copy under a new service

        It's important that we set pk to None so a new object is created, but we
        also need to ensure the new name is unique by appending some unique data
        to the end of the name
        '''
        with transaction.atomic():
            content_type = ContentType.objects.get(model=content_type, app_label='promgen')

            # First check to see if this rule is already overwritten
            for rule in Rule.objects.filter(parent_id=self.pk, content_type=content_type, object_id=object_id):
                return rule

            content_object = content_type.get_object_for_this_type(pk=object_id)

            orig_pk = self.pk
            self.pk = None
            self.parent_id = orig_pk
            self.name = '{}_{}'.format(self.name, slugify(content_object.name)).replace('-', '_')
            self.content_type = content_type
            self.object_id = object_id
            self.enabled = False
            self.clause = self.clause.replace(macro.EXCLUSION_MACRO, '{}="{}",{}'.format(
                content_type.model, content_object.name, macro.EXCLUSION_MACRO
            ))
            self.save()

            # Add a label to our new rule by default, to help ensure notifications
            # get routed to the notifier we expect
            self.add_label(content_type.model, content_object.name)

            for label in RuleLabel.objects.filter(rule_id=orig_pk):
                # Skip service labels from our previous rule
                if label.name in ['service', 'project']:
                    logger.debug('Skipping %s: %s', label.name, label.value)
                    continue
                logger.debug('Copying %s to %s', label, self)
                label.pk = None
                label.rule = self
                label.save()

            for annotation in RuleAnnotation.objects.filter(rule_id=orig_pk):
                logger.debug('Copying %s to %s', annotation, self)
                annotation.pk = None
                annotation.rule = self
                annotation.save()

        return self


class RuleLabel(models.Model):
    name = models.CharField(max_length=128)
    value = models.CharField(max_length=128)
    rule = models.ForeignKey('Rule', on_delete=models.CASCADE)


class RuleAnnotation(models.Model):
    name = models.CharField(max_length=128)
    value = models.TextField()
    rule = models.ForeignKey('Rule', on_delete=models.CASCADE)


class Alert(models.Model):
    created = models.DateTimeField(default=timezone.now)
    body = models.TextField()

    def expand(self):
        # Map of Prometheus labels to Promgen objects
        LABEL_MAPPING = [
            ('project', Project),
            ('service', Service),
        ]
        routable = {}
        data = self.json()

        data.setdefault('commonLabels', {})
        data.setdefault('commonAnnotations', {})

        # Look through our labels and find the object from Promgen's DB
        # If we find an object in Promgen, add an annotation with a direct link
        for label, klass in LABEL_MAPPING:
            if label not in data['commonLabels']:
                logger.debug('Missing label %s', label)
                continue

            # Should only find a single value, but I think filter is a little
            # bit more forgiving than get in terms of throwing errors
            for obj in klass.objects.filter(name=data['commonLabels'][label]):
                logger.debug('Found %s %s', label, obj)
                routable[label] = obj
                data['commonAnnotations'][label] = resolve_domain(obj)

        return routable, data

    @cached_property
    def json(self):
        return json.loads(self.body)


class Audit(models.Model):
    body = models.TextField()
    created = models.DateTimeField()
    data = models.TextField(blank=True)
    old = models.TextField(blank=True)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True)
    object_id = models.PositiveIntegerField(default=0)
    content_object = GenericForeignKey('content_type', 'object_id')

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, default=None)

    @property
    def hilight(self):
        if self.body.startswith('Created'):
            return 'success'
        if self.body.startswith('Updated'):
            return 'warning'
        if self.body.startswith('Deleted'):
            return 'danger'
        return ''

    @classmethod
    def log(cls, body, instance=None, old=None, **kwargs):
        from promgen.middleware import get_current_user

        kwargs['body'] = body
        kwargs['created'] = timezone.now()
        kwargs['user'] = get_current_user()

        if instance:
            kwargs['content_type'] = ContentType.objects.get_for_model(instance)
            kwargs['object_id'] = instance.id
            kwargs['data'] = json.dumps(model_to_dict(instance), sort_keys=True)
        if old:
            kwargs['old'] = json.dumps(model_to_dict(old), sort_keys=True)

        return cls.objects.create(**kwargs)


class Prometheus(models.Model):
    shard = models.ForeignKey('Shard', on_delete=models.CASCADE)
    host = models.CharField(max_length=128)
    port = models.IntegerField()

    def __str__(self):
        return '{}:{}'.format(self.host, self.port)

    class Meta:
        ordering = ['shard', 'host']
        unique_together = (('host', 'port',),)
        verbose_name_plural = 'prometheis'
