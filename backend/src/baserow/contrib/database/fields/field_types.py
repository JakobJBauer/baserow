from decimal import Decimal
from pytz import timezone
from random import randrange, randint
from dateutil import parser
from dateutil.parser import ParserError
from datetime import datetime, date

from django.db import models
from django.db.models import Case, When
from django.contrib.postgres.fields import JSONField
from django.core.validators import URLValidator, EmailValidator
from django.core.exceptions import ValidationError
from django.utils.timezone import make_aware

from rest_framework import serializers

from baserow.core.models import UserFile
from baserow.core.user_files.exceptions import UserFileDoesNotExist
from baserow.contrib.database.api.fields.serializers import (
    LinkRowValueSerializer, FileFieldRequestSerializer, FileFieldResponseSerializer,
    SelectOptionSerializer
)
from baserow.contrib.database.api.fields.errors import (
    ERROR_LINK_ROW_TABLE_NOT_IN_SAME_DATABASE, ERROR_LINK_ROW_TABLE_NOT_PROVIDED,
    ERROR_INCOMPATIBLE_PRIMARY_FIELD_TYPE
)

from .handler import FieldHandler
from .registries import FieldType, field_type_registry
from .models import (
    NUMBER_TYPE_INTEGER, NUMBER_TYPE_DECIMAL, DATE_FORMAT, DATE_TIME_FORMAT,
    TextField, LongTextField, URLField, NumberField, BooleanField, DateField,
    LinkRowField, EmailField, FileField,
    SingleSelectField, SelectOption
)
from .exceptions import (
    LinkRowTableNotInSameDatabase, LinkRowTableNotProvided,
    IncompatiblePrimaryFieldTypeError
)
from .fields import SingleSelectForeignKey


class TextFieldType(FieldType):
    type = 'text'
    model_class = TextField
    allowed_fields = ['text_default']
    serializer_field_names = ['text_default']

    def get_serializer_field(self, instance, **kwargs):
        return serializers.CharField(required=False, allow_null=True, allow_blank=True,
                                     default=instance.text_default or None, **kwargs)

    def get_model_field(self, instance, **kwargs):
        return models.TextField(default=instance.text_default or None, blank=True,
                                null=True, **kwargs)

    def random_value(self, instance, fake, cache):
        return fake.name()


class LongTextFieldType(FieldType):
    type = 'long_text'
    model_class = LongTextField

    def get_serializer_field(self, instance, **kwargs):
        return serializers.CharField(required=False, allow_null=True, allow_blank=True,
                                     **kwargs)

    def get_model_field(self, instance, **kwargs):
        return models.TextField(blank=True, null=True, **kwargs)

    def random_value(self, instance, fake, cache):
        return fake.text()


class URLFieldType(FieldType):
    type = 'url'
    model_class = URLField

    def prepare_value_for_db(self, instance, value):
        if value == '' or value is None:
            return ''

        validator = URLValidator()
        validator(value)
        return value

    def get_serializer_field(self, instance, **kwargs):
        return serializers.URLField(required=False, allow_null=True, allow_blank=True,
                                    **kwargs)

    def get_model_field(self, instance, **kwargs):
        return models.URLField(default='', blank=True, null=True, **kwargs)

    def random_value(self, instance, fake, cache):
        return fake.url()

    def get_alter_column_prepare_new_value(self, connection, from_field, to_field):
        if connection.vendor == 'postgresql':
            return r"""p_in = (
            case
                when p_in::text ~* '(https?|ftps?)://(-\.)?([^\s/?\.#-]+\.?)+(/[^\s]*)?'
                then p_in::text
                else ''
                end
            );"""

        return super().get_alter_column_prepare_new_value(connection, from_field,
                                                          to_field)


class NumberFieldType(FieldType):
    MAX_DIGITS = 50

    type = 'number'
    model_class = NumberField
    allowed_fields = ['number_type', 'number_decimal_places', 'number_negative']
    serializer_field_names = ['number_type', 'number_decimal_places', 'number_negative']

    def prepare_value_for_db(self, instance, value):
        if value is not None:
            value = Decimal(value)

        if value is not None and not instance.number_negative and value < 0:
            raise ValidationError(f'The value for field {instance.id} cannot be '
                                  f'negative.')
        return value

    def get_serializer_field(self, instance, **kwargs):
        kwargs['decimal_places'] = (
            0
            if instance.number_type == NUMBER_TYPE_INTEGER else
            instance.number_decimal_places
        )

        if not instance.number_negative:
            kwargs['min_value'] = 0

        return serializers.DecimalField(
            max_digits=self.MAX_DIGITS + kwargs['decimal_places'],
            required=False,
            allow_null=True,
            **kwargs
        )

    def get_model_field(self, instance, **kwargs):
        kwargs['decimal_places'] = (
            0
            if instance.number_type == NUMBER_TYPE_INTEGER else
            instance.number_decimal_places
        )

        return models.DecimalField(
            max_digits=self.MAX_DIGITS + kwargs['decimal_places'],
            null=True,
            blank=True,
            **kwargs
        )

    def random_value(self, instance, fake, cache):
        if instance.number_type == NUMBER_TYPE_INTEGER:
            return fake.pyint(
                min_value=-10000 if instance.number_negative else 0,
                max_value=10000,
                step=1
            )
        elif instance.number_type == NUMBER_TYPE_DECIMAL:
            return fake.pydecimal(
                min_value=-10000 if instance.number_negative else 0,
                max_value=10000,
                positive=not instance.number_negative
            )

    def get_alter_column_prepare_new_value(self, connection, from_field, to_field):
        if connection.vendor == 'postgresql':
            decimal_places = 0
            if to_field.number_type == NUMBER_TYPE_DECIMAL:
                decimal_places = to_field.number_decimal_places

            function = f"round(p_in::numeric, {decimal_places})"

            if not to_field.number_negative:
                function = f"greatest({function}, 0)"

            return f'p_in = {function};'

        return super().get_alter_column_prepare_new_value(connection, from_field,
                                                          to_field)

    def after_update(self, from_field, to_field, from_model, to_model, user, connection,
                     altered_column, before):
        """
        The allowing of negative values isn't stored in the database field type. If
        the type hasn't changed, but the allowing of negative values has it means that
        the column data hasn't been converted to positive values yet. We need to do
        this here. All the negatives values are set to 0.
        """

        if (
            not altered_column
            and not to_field.number_negative
            and from_field.number_negative
        ):
            to_model.objects.filter(**{
                f'field_{to_field.id}__lt': 0
            }).update(**{
                f'field_{to_field.id}': 0
            })


class BooleanFieldType(FieldType):
    type = 'boolean'
    model_class = BooleanField

    def get_serializer_field(self, instance, **kwargs):
        return serializers.BooleanField(required=False, default=False, **kwargs)

    def get_model_field(self, instance, **kwargs):
        return models.BooleanField(default=False, **kwargs)

    def random_value(self, instance, fake, cache):
        return fake.pybool()


class DateFieldType(FieldType):
    type = 'date'
    model_class = DateField
    allowed_fields = ['date_format', 'date_include_time', 'date_time_format']
    serializer_field_names = ['date_format', 'date_include_time', 'date_time_format']

    def prepare_value_for_db(self, instance, value):
        """
        This method accepts a string, date object or datetime object. If the value is a
        string it will try to parse it using the dateutil's parser. Depending on the
        field's date_include_time, a date or datetime object will be returned. A
        datetime object will always have a UTC timezone. If the value is a datetime
        object with another timezone it will be converted to UTC.

        :param instance: The date field instance for which the value needs to be
            prepared.
        :type instance: DateField
        :param value: The value that needs to be prepared.
        :type value: str, date or datetime
        :return: The date or datetime field with the correct value.
        :rtype: date or datetime(tzinfo=UTC)
        :raises ValidationError: When the provided date string could not be converted
            to a date object.
        """

        if not value:
            return value

        utc = timezone('UTC')

        if type(value) == str:
            try:
                value = parser.parse(value)
            except ParserError:
                raise ValidationError('The provided string could not converted to a'
                                      'date.')

        if type(value) == date:
            value = make_aware(datetime(value.year, value.month, value.day), utc)

        if type(value) == datetime:
            value = value.astimezone(utc)
            return value if instance.date_include_time else value.date()

        raise ValidationError('The value should be a date/time string, date object or '
                              'datetime object.')

    def get_serializer_field(self, instance, **kwargs):
        kwargs['required'] = False
        kwargs['allow_null'] = True
        if instance.date_include_time:
            return serializers.DateTimeField(**kwargs)
        else:
            return serializers.DateField(**kwargs)

    def get_model_field(self, instance, **kwargs):
        kwargs['null'] = True
        kwargs['blank'] = True
        if instance.date_include_time:
            return models.DateTimeField(**kwargs)
        else:
            return models.DateField(**kwargs)

    def random_value(self, instance, fake, cache):
        if instance.date_include_time:
            return make_aware(fake.date_time())
        else:
            return fake.date_object()

    def get_alter_column_prepare_old_value(self, connection, from_field, to_field):
        """
        If the field type has changed then we want to convert the date or timestamp to
        a human readable text following the old date format.
        """

        to_field_type = field_type_registry.get_by_model(to_field)
        if to_field_type.type != self.type and connection.vendor == 'postgresql':
            sql_type = 'date'
            sql_format = DATE_FORMAT[from_field.date_format]['sql']

            if from_field.date_include_time:
                sql_type = 'timestamp'
                sql_format += ' ' + DATE_TIME_FORMAT[from_field.date_time_format]['sql']

            return f"""p_in = TO_CHAR(p_in::{sql_type}, '{sql_format}');"""

        return super().get_alter_column_prepare_old_value(connection, from_field,
                                                          to_field)

    def get_alter_column_prepare_new_value(self, connection, from_field, to_field):
        """
        If the field type has changed into a date field then we want to parse the old
        text value following the format of the new field and convert it to a date or
        timestamp. If that fails we want to fallback on the default ::date or
        ::timestamp conversion that has already been added.
        """

        from_field_type = field_type_registry.get_by_model(from_field)
        if from_field_type.type != self.type and connection.vendor == 'postgresql':
            sql_function = 'TO_DATE'
            sql_format = DATE_FORMAT[to_field.date_format]['sql']

            if to_field.date_include_time:
                sql_function = 'TO_TIMESTAMP'
                sql_format += ' ' + DATE_TIME_FORMAT[to_field.date_time_format]['sql']

            return f"""
                begin
                    p_in = {sql_function}(p_in::text, 'FM{sql_format}');
                exception when others then end;
            """

        return super().get_alter_column_prepare_old_value(connection, from_field,
                                                          to_field)


class LinkRowFieldType(FieldType):
    """
    The link row field can be used to link a field to a row of another table. Because
    the user should also be able to see which rows are linked to the related table,
    another link row field in the related table is automatically created.
    """

    type = 'link_row'
    model_class = LinkRowField
    allowed_fields = ['link_row_table', 'link_row_related_field',
                      'link_row_relation_id']
    serializer_field_names = ['link_row_table', 'link_row_related_field']
    serializer_field_overrides = {
        'link_row_related_field': serializers.PrimaryKeyRelatedField(read_only=True)
    }
    api_exceptions_map = {
        LinkRowTableNotProvided: ERROR_LINK_ROW_TABLE_NOT_PROVIDED,
        LinkRowTableNotInSameDatabase: ERROR_LINK_ROW_TABLE_NOT_IN_SAME_DATABASE,
        IncompatiblePrimaryFieldTypeError: ERROR_INCOMPATIBLE_PRIMARY_FIELD_TYPE
    }
    can_order_by = False
    can_be_primary_field = False

    def enhance_queryset(self, queryset, field, name):
        """
        Makes sure that the related rows are prefetched by Django. We also want to
        enhance the primary field of the related queryset. If for example the primary
        field is a single select field then the dropdown options need to be
        prefetched in order to prevent many queries.
        """

        remote_model = queryset.model._meta.get_field(name).remote_field.model
        related_queryset = remote_model.objects.all()

        try:
            primary_field_object = next(
                object
                for object in remote_model._field_objects.values()
                if object['field'].primary
            )
            related_queryset = primary_field_object['type'].enhance_queryset(
                related_queryset,
                primary_field_object['field'],
                primary_field_object['name']
            )
        except StopIteration:
            # If the related model does not have a primary field then we also don't
            # need to enhance the queryset.
            pass

        return queryset.prefetch_related(models.Prefetch(
            name,
            queryset=related_queryset
        ))

    def get_serializer_field(self, instance, **kwargs):
        """
        If the value is going to be updated we want to accept a list of integers
        representing the related row ids.
        """

        return serializers.ListField(child=serializers.IntegerField(min_value=0),
                                     required=False, **kwargs)

    def get_response_serializer_field(self, instance, **kwargs):
        """
        If a model has already been generated it will be added as a property to the
        instance. If that is the case then we can extract the primary field from the
        model and we can pass the name along to the LinkRowValueSerializer. It will
        be used to include the primary field's value in the response as a string.
        """

        primary_field_name = None

        if hasattr(instance, '_related_model'):
            related_model = instance._related_model
            primary_field = next(
                object
                for object in related_model._field_objects.values()
                if object['field'].primary
            )
            if primary_field:
                primary_field_name = primary_field['name']

        return serializers.ListSerializer(child=LinkRowValueSerializer(
            value_field_name=primary_field_name, required=False, **kwargs
        ))

    def get_serializer_help_text(self, instance):
        return 'This field accepts an `array` containing the ids of the related rows.' \
               'The response contains a list of objects containing the `id` and ' \
               'the primary field\'s `value` as a string for display purposes.'

    def get_model_field(self, instance, **kwargs):
        """
        A model field is not needed because the ManyToMany field is going to be added
        after the model has been generated.
        """

        return None

    def after_model_generation(self, instance, model, field_name, manytomany_models):
        # Store the current table's model into the manytomany_models object so that the
        # related ManyToMany field can use that one. Otherwise we end up in a recursive
        # loop.
        manytomany_models[instance.table.id] = model

        # Check if the related table model is already in the manytomany_models.
        related_model = manytomany_models.get(instance.link_row_table.id)

        # If we do not have a related table model already we can generate a new one.
        if not related_model:
            related_model = instance.link_row_table.get_model(
                manytomany_models=manytomany_models
            )

        instance._related_model = related_model
        related_name = f'reversed_field_{instance.id}'

        # Try to find the related field in the related model in order to figure out what
        # the related name should be. If the related if is not found that means that it
        # has not yet been created.
        for related_field in related_model._field_objects.values():
            if (
                isinstance(related_field['field'], self.model_class) and
                related_field['field'].link_row_related_field and
                related_field['field'].link_row_related_field.id == instance.id
            ):
                related_name = related_field['name']

        # Note that the through model will not be registered with the apps because of
        # the `DatabaseConfig.prevent_generated_model_for_registering` hack.

        models.ManyToManyField(
            to=related_model,
            related_name=related_name,
            null=True,
            blank=True,
            db_table=instance.through_table_name,
            db_constraint=False
        ).contribute_to_class(
            model,
            field_name
        )

        model_field = model._meta.get_field(field_name)
        model_field.do_related_class(
            model_field.remote_field.model,
            None
        )

    def prepare_values(self, values, user):
        """
        This method checks if the provided link row table is an int because then it
        needs to be converted to a table instance.
        """

        if 'link_row_table' in values and isinstance(values['link_row_table'], int):
            from baserow.contrib.database.table.handler import TableHandler

            table = TableHandler().get_table(values['link_row_table'])
            table.database.group.has_user(user, raise_error=True)
            values['link_row_table'] = table

        return values

    def before_create(self, table, primary, values, order, user):
        """
        It is not allowed to link with a table from another database. This method
        checks if the database ids are the same and if not a proper exception is
        raised.
        """

        if 'link_row_table' not in values or not values['link_row_table']:
            raise LinkRowTableNotProvided(
                'The link_row_table argument must be provided when creating a link_row '
                'field.'
            )

        link_row_table = values['link_row_table']

        if table.database_id != link_row_table.database_id:
            raise LinkRowTableNotInSameDatabase(
                f'The link row table {link_row_table.id} is not in the same database '
                f'as the table {table.id}.'
            )

    def before_update(self, from_field, to_field_values, user):
        """
        It is not allowed to link with a table from another database if the
        link_row_table has changed and if it is within the same database.
        """

        if (
            'link_row_table' not in to_field_values or
            not to_field_values['link_row_table']
        ):
            return

        link_row_table = to_field_values['link_row_table']
        table = from_field.table

        if from_field.table.database_id != link_row_table.database_id:
            raise LinkRowTableNotInSameDatabase(
                f'The link row table {link_row_table.id} is not in the same database '
                f'as the table {table.id}.'
            )

    def after_create(self, field, model, user, connection, before):
        """
        When the field is created we have to add the related field to the related
        table so a reversed lookup can be done by the user.
        """

        if field.link_row_related_field:
            return

        field.link_row_related_field = FieldHandler().create_field(
            user=user,
            table=field.link_row_table,
            type_name=self.type,
            do_schema_change=False,
            name=field.table.name,
            link_row_table=field.table,
            link_row_related_field=field,
            link_row_relation_id=field.link_row_relation_id
        )
        field.save()

    def before_schema_change(self, from_field, to_field, to_model, from_model,
                             from_model_field, to_model_field, user):
        if not isinstance(to_field, self.model_class):
            # If we are not going to convert to another manytomany field the
            # related field can be deleted.
            from_field.link_row_related_field.delete()
        elif (
            isinstance(to_field, self.model_class) and
            isinstance(from_field, self.model_class) and
            to_field.link_row_table.id != from_field.link_row_table.id
        ):
            # If the table has changed we have to change the following data in the
            # related field
            from_field.link_row_related_field.name = to_field.table.name
            from_field.link_row_related_field.table = to_field.link_row_table
            from_field.link_row_related_field.link_row_table = to_field.table
            from_field.link_row_related_field.order = self.model_class.get_last_order(
                to_field.link_row_table
            )
            from_field.link_row_related_field.save()

    def after_update(self, from_field, to_field, from_model, to_model, user, connection,
                     altered_column, before):
        """
        If the old field is not already a link row field we have to create the related
        field into the related table.
        """

        if (
            not isinstance(from_field, self.model_class) and
            isinstance(to_field, self.model_class)
        ):
            to_field.link_row_related_field = FieldHandler().create_field(
                user=user,
                table=to_field.link_row_table,
                type_name=self.type,
                do_schema_change=False,
                name=to_field.table.name,
                link_row_table=to_field.table,
                link_row_related_field=to_field,
                link_row_relation_id=to_field.link_row_relation_id
            )
            to_field.save()

    def after_delete(self, field, model, user, connection):
        """
        After the field has been deleted we also need to delete the related field.
        """

        field.link_row_related_field.delete()

    def random_value(self, instance, fake, cache):
        """
        Selects a between 0 and 3 random rows from the instance's link row table and
        return those ids in a list.
        """

        model_name = f'table_{instance.link_row_table.id}'
        count_name = f'table_{instance.link_row_table.id}_count'

        if model_name not in cache:
            cache[model_name] = instance.link_row_table.get_model()
            cache[count_name] = cache[model_name].objects.all().count()

        model = cache[model_name]
        count = cache[count_name]
        values = []

        if count == 0:
            return values

        for i in range(0, randrange(0, 3)):
            instance = model.objects.all()[randint(0, count - 1)]
            values.append(instance.id)

        return values


class EmailFieldType(FieldType):
    type = 'email'
    model_class = EmailField

    def prepare_value_for_db(self, instance, value):
        if value == '' or value is None:
            return ''

        validator = EmailValidator()
        validator(value)
        return value

    def get_serializer_field(self, instance, **kwargs):
        return serializers.EmailField(
            required=False,
            allow_null=True,
            allow_blank=True,
            **kwargs
        )

    def get_model_field(self, instance, **kwargs):
        return models.EmailField(default='', blank=True, null=True, **kwargs)

    def random_value(self, instance, fake, cache):
        return fake.email()

    def get_alter_column_prepare_new_value(self, connection, from_field, to_field):
        if connection.vendor == 'postgresql':
            return r"""p_in = (
            case
                when p_in::text ~* '[A-Z0-9._+-]+@[A-Z0-9.-]+\.[A-Z]{2,}'
                then p_in::text
                else ''
                end
            );"""

        return super().get_alter_column_prepare_new_value(connection, from_field,
                                                          to_field)


class FileFieldType(FieldType):
    type = 'file'
    model_class = FileField

    def prepare_value_for_db(self, instance, value):
        if value is None:
            return []

        if not isinstance(value, list):
            raise ValidationError('The provided value must be a list.')

        if len(value) == 0:
            return []

        # Validates the provided object and extract the names from it. We need the name
        # to validate if the file actually exists and to get the 'real' properties
        # from it.
        provided_files = []
        for o in value:
            if not isinstance(o, object) or not isinstance(o.get('name'), str):
                raise ValidationError('Every provided value must at least contain '
                                      'the file name as `name`.')

            if 'visible_name' in o and not isinstance(o['visible_name'], str):
                raise ValidationError('The provided `visible_name` must be a string.')

            provided_files.append(o)

        # Create a list of the serialized UserFiles in the originally provided order
        # because that is also the order we need to store the serialized versions in.
        user_files = []
        queryset = UserFile.objects.all().name(*[f['name'] for f in provided_files])
        for file in provided_files:
            try:
                user_file = next(
                    user_file
                    for user_file in queryset
                    if user_file.name == file['name']
                )
                serialized = user_file.serialize()
                serialized['visible_name'] = (
                    file.get('visible_name') or user_file.original_name
                )
            except StopIteration:
                raise UserFileDoesNotExist(
                    file['name'],
                    f"The provided file {file['name']} does not exist."
                )

            user_files.append(serialized)

        return user_files

    def get_serializer_field(self, instance, **kwargs):
        return serializers.ListSerializer(
            child=FileFieldRequestSerializer(),
            required=False,
            allow_null=True,
            **kwargs
        )

    def get_response_serializer_field(self, instance, **kwargs):
        return FileFieldResponseSerializer(many=True, required=False, **kwargs)

    def get_serializer_help_text(self, instance):
        return 'This field accepts an `array` containing objects with the name of ' \
               'the file. The response contains an `array` of more detailed objects ' \
               'related to the files.'

    def get_model_field(self, instance, **kwargs):
        return JSONField(default=[], **kwargs)

    def random_value(self, instance, fake, cache):
        """
        Selects between 0 and 3 random user files and returns those serialized in a
        list.
        """

        count_name = f'field_{instance.id}_count'

        if count_name not in cache:
            cache[count_name] = UserFile.objects.all().count()

        values = []
        count = cache[count_name]

        if count == 0:
            return values

        for i in range(0, randrange(0, 3)):
            instance = UserFile.objects.all()[randint(0, count - 1)]
            serialized = instance.serialize()
            serialized['visible_name'] = serialized['name']
            values.append(serialized)

        return values


class SingleSelectFieldType(FieldType):
    type = 'single_select'
    model_class = SingleSelectField
    can_have_select_options = True
    allowed_fields = ['select_options']
    serializer_field_names = ['select_options']
    serializer_field_overrides = {
        'select_options': SelectOptionSerializer(many=True, required=False)
    }

    def enhance_queryset(self, queryset, field, name):
        return queryset.prefetch_related(
            models.Prefetch(name, queryset=SelectOption.objects.using('default').all())
        )

    def prepare_value_for_db(self, instance, value):
        if value is None:
            return value

        if isinstance(value, int):
            try:
                return SelectOption.objects.get(field=instance, id=value)
            except SelectOption.DoesNotExist:
                pass

        if isinstance(value, SelectOption) and value.field_id == instance.id:
            return value

        # If the select option is not found or if it does not belong to the right field
        # then the provided value is invalid and a validation error can be raised.
        raise ValidationError(f'The provided value is not a valid option.')

    def get_serializer_field(self, instance, **kwargs):
        return serializers.PrimaryKeyRelatedField(
            queryset=SelectOption.objects.filter(field=instance), required=False,
            allow_null=True, **kwargs
        )

    def get_response_serializer_field(self, instance, **kwargs):
        return SelectOptionSerializer(required=False, allow_null=True, **kwargs)

    def get_serializer_help_text(self, instance):
        return (
            'This field accepts an `integer` representing the chosen select option id '
            'related to the field. Available ids can be found when getting or listing '
            'the field. The response represents chosen field, but also the value and '
            'color is exposed.'
        )

    def get_model_field(self, instance, **kwargs):
        return SingleSelectForeignKey(
            to=SelectOption,
            on_delete=models.SET_NULL,
            related_name='+',
            related_query_name='+',
            db_constraint=False,
            null=True,
            blank=True,
            **kwargs
        )

    def before_create(self, table, primary, values, order, user):
        if 'select_options' in values:
            return values.pop('select_options')

    def after_create(self, field, model, user, connection, before):
        if before and len(before) > 0:
            FieldHandler().update_field_select_options(user, field, before)

    def before_update(self, from_field, to_field_values, user):
        if 'select_options' in to_field_values:
            FieldHandler().update_field_select_options(
                user,
                from_field,
                to_field_values['select_options']
            )
            to_field_values.pop('select_options')

    def get_alter_column_prepare_old_value(self, connection, from_field, to_field):
        """
        If the new field type isn't a single select field we can convert the plain
        text value of the option and maybe that can be used by the new field.
        """

        to_field_type = field_type_registry.get_by_model(to_field)
        if to_field_type.type != self.type and connection.vendor == 'postgresql':
            variables = {}
            values_mapping = []
            for option in from_field.select_options.all():
                variable_name = f'option_{option.id}_value'
                variables[variable_name] = option.value
                values_mapping.append(f"('{int(option.id)}', %({variable_name})s)")

            # If there are no values we don't need to convert the value to a string
            # since all values will be converted to null.
            if len(values_mapping) == 0:
                return None

            sql = f"""
                p_in = (SELECT value FROM (
                    VALUES {','.join(values_mapping)}
                ) AS values (key, value)
                WHERE key = p_in);
            """
            return sql, variables

        return super().get_alter_column_prepare_old_value(connection, from_field,
                                                          to_field)

    def get_alter_column_prepare_new_value(self, connection, from_field, to_field):
        """
        If the old field wasn't a single select field we can try to match the old text
        values to the new options.
        """

        from_field_type = field_type_registry.get_by_model(from_field)
        if from_field_type.type != self.type and connection.vendor == 'postgresql':
            variables = {}
            values_mapping = []
            for option in to_field.select_options.all():
                variable_name = f'option_{option.id}_value'
                variables[variable_name] = option.value
                values_mapping.append(
                    f"(lower(%({variable_name})s), '{int(option.id)}')"
                )

            # If there are no values we don't need to convert the value since all
            # values should be converted to null.
            if len(values_mapping) == 0:
                return None

            return f"""p_in = (
                SELECT value FROM (
                    VALUES {','.join(values_mapping)}
                ) AS values (key, value)
                WHERE key = lower(p_in)
            );
            """, variables

        return super().get_alter_column_prepare_old_value(connection, from_field,
                                                          to_field)

    def get_order(self, field, field_name, view_sort):
        """
        If the user wants to sort the results he expects them to be ordered
        alphabetically based on the select option value and not in the id which is
        stored in the table. This method generates a Case expression which maps the id
        to the correct position.
        """

        select_options = field.select_options.all().order_by('value')
        options = [select_option.pk for select_option in select_options]
        options.insert(0, None)

        if view_sort.order == 'DESC':
            options.reverse()

        order = Case(*[
            When(**{field_name: option, 'then': index})
            for index, option in enumerate(options)
        ])
        return order

    def random_value(self, instance, fake, cache):
        """
        Selects a random choice out of the possible options.
        """

        cache_entry_name = f'field_{instance.id}_options'

        if cache_entry_name not in cache:
            cache[cache_entry_name] = instance.select_options.all()

        select_options = cache[cache_entry_name]

        # if the select_options are empty return None
        if not select_options:
            return None

        random_choice = randint(0, len(select_options) - 1)

        return select_options[random_choice]
