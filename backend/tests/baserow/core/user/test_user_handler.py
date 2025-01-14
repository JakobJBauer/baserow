import pytest
from decimal import Decimal
from unittest.mock import MagicMock
from freezegun import freeze_time

from itsdangerous.exc import SignatureExpired, BadSignature

from django.contrib.auth import get_user_model

from baserow.core.models import Group, GroupUser
from baserow.core.registries import plugin_registry
from baserow.contrib.database.models import (
    Database, Table, GridView, TextField, LongTextField, BooleanField, DateField
)
from baserow.contrib.database.views.models import GridViewFieldOptions
from baserow.core.exceptions import (
    BaseURLHostnameNotAllowed, GroupInvitationEmailMismatch,
    GroupInvitationDoesNotExist
)
from baserow.core.handler import CoreHandler
from baserow.core.user.exceptions import (
    UserAlreadyExist, UserNotFound, InvalidPassword, DisabledSignupError
)
from baserow.core.user.handler import UserHandler


User = get_user_model()


@pytest.mark.django_db
def test_get_user(data_fixture):
    user_1 = data_fixture.create_user(email='user1@localhost')

    handler = UserHandler()

    with pytest.raises(ValueError):
        handler.get_user()

    with pytest.raises(UserNotFound):
        handler.get_user(user_id=-1)

    with pytest.raises(UserNotFound):
        handler.get_user(email='user3@localhost')

    assert handler.get_user(user_id=user_1.id).id == user_1.id
    assert handler.get_user(email=user_1.email).id == user_1.id


@pytest.mark.django_db
def test_create_user(data_fixture):
    plugin_mock = MagicMock()
    plugin_registry.registry['mock'] = plugin_mock

    user_handler = UserHandler()

    data_fixture.update_settings(allow_new_signups=False)
    with pytest.raises(DisabledSignupError):
        user_handler.create_user('Test1', 'test@test.nl', 'password')
    assert User.objects.all().count() == 0
    data_fixture.update_settings(allow_new_signups=True)

    user = user_handler.create_user('Test1', 'test@test.nl', 'password')
    assert user.pk
    assert user.first_name == 'Test1'
    assert user.email == 'test@test.nl'
    assert user.username == 'test@test.nl'

    assert Group.objects.all().count() == 1
    group = Group.objects.all().first()
    assert group.users.filter(id=user.id).count() == 1
    assert group.name == "Test1's group"

    assert Database.objects.all().count() == 1
    assert Table.objects.all().count() == 2
    assert GridView.objects.all().count() == 2
    assert TextField.objects.all().count() == 3
    assert LongTextField.objects.all().count() == 1
    assert BooleanField.objects.all().count() == 2
    assert DateField.objects.all().count() == 1
    assert GridViewFieldOptions.objects.all().count() == 3

    tables = Table.objects.all().order_by('id')

    model_1 = tables[0].get_model()
    model_1_results = model_1.objects.all()
    assert len(model_1_results) == 4
    assert model_1_results[0].order == Decimal('1.00000000000000000000')
    assert model_1_results[1].order == Decimal('2.00000000000000000000')
    assert model_1_results[2].order == Decimal('3.00000000000000000000')
    assert model_1_results[3].order == Decimal('4.00000000000000000000')

    model_2 = tables[1].get_model()
    model_2_results = model_2.objects.all()
    assert len(model_2_results) == 3
    assert model_2_results[0].order == Decimal('1.00000000000000000000')
    assert model_2_results[1].order == Decimal('2.00000000000000000000')
    assert model_2_results[2].order == Decimal('3.00000000000000000000')

    plugin_mock.user_created.assert_called_with(user, group, None)

    with pytest.raises(UserAlreadyExist):
        user_handler.create_user('Test1', 'test@test.nl', 'password')


@pytest.mark.django_db
def test_create_user_with_invitation(data_fixture):
    plugin_mock = MagicMock()
    plugin_registry.registry['mock'] = plugin_mock

    user_handler = UserHandler()
    core_handler = CoreHandler()

    invitation = data_fixture.create_group_invitation(email='test0@test.nl')
    signer = core_handler.get_group_invitation_signer()

    with pytest.raises(BadSignature):
        user_handler.create_user('Test1', 'test0@test.nl', 'password', 'INVALID')

    with pytest.raises(GroupInvitationDoesNotExist):
        user_handler.create_user('Test1', 'test0@test.nl', 'password',
                                 signer.dumps(99999))

    with pytest.raises(GroupInvitationEmailMismatch):
        user_handler.create_user('Test1', 'test1@test.nl', 'password',
                                 signer.dumps(invitation.id))

    user = user_handler.create_user('Test1', 'test0@test.nl', 'password',
                                    signer.dumps(invitation.id))

    assert Group.objects.all().count() == 1
    assert Group.objects.all().first().id == invitation.group_id
    assert GroupUser.objects.all().count() == 2

    plugin_mock.user_created.assert_called_once()
    args = plugin_mock.user_created.call_args
    assert args[0][0] == user
    assert args[0][1].id == invitation.group_id
    assert args[0][2].email == invitation.email
    assert args[0][2].group_id == invitation.group_id

    # We do not expect any initial data to have been created.
    assert Database.objects.all().count() == 0
    assert Table.objects.all().count() == 0


@pytest.mark.django_db
def test_send_reset_password_email(data_fixture, mailoutbox):
    user = data_fixture.create_user(email='test@localhost')
    handler = UserHandler()

    with pytest.raises(BaseURLHostnameNotAllowed):
        handler.send_reset_password_email(user, 'http://test.nl/reset-password')

    signer = handler.get_reset_password_signer()
    handler.send_reset_password_email(user, 'http://localhost:3000/reset-password')

    assert len(mailoutbox) == 1
    email = mailoutbox[0]

    assert email.subject == 'Reset password - Baserow'
    assert email.from_email == 'no-reply@localhost'
    assert 'test@localhost' in email.to

    html_body = email.alternatives[0][0]
    search_url = 'http://localhost:3000/reset-password/'
    start_url_index = html_body.index(search_url)

    assert start_url_index != -1

    end_url_index = html_body.index('"', start_url_index)
    token = html_body[start_url_index + len(search_url):end_url_index]

    user_id = signer.loads(token)
    assert user_id == user.id


@pytest.mark.django_db
def test_reset_password(data_fixture):
    user = data_fixture.create_user(email='test@localhost')
    handler = UserHandler()

    signer = handler.get_reset_password_signer()

    with pytest.raises(BadSignature):
        handler.reset_password('test', 'test')
        assert not user.check_password('test')

    with freeze_time('2020-01-01 12:00'):
        token = signer.dumps(9999)

    with freeze_time('2020-01-02 12:00'):
        with pytest.raises(UserNotFound):
            handler.reset_password(token, 'test')
            assert not user.check_password('test')

    with freeze_time('2020-01-01 12:00'):
        token = signer.dumps(user.id)

    with freeze_time('2020-01-04 12:00'):
        with pytest.raises(SignatureExpired):
            handler.reset_password(token, 'test')
            assert not user.check_password('test')

    with freeze_time('2020-01-02 12:00'):
        user = handler.reset_password(token, 'test')
        assert user.check_password('test')


@pytest.mark.django_db
def test_change_password(data_fixture):
    user = data_fixture.create_user(email='test@localhost', password='test')
    handler = UserHandler()

    with pytest.raises(InvalidPassword):
        handler.change_password(user, 'INCORRECT', 'new')

    user.refresh_from_db()
    assert user.check_password('test')

    user = handler.change_password(user, 'test', 'new')
    assert user.check_password('new')
