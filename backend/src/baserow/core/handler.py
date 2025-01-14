from urllib.parse import urlparse, urljoin
from itsdangerous import URLSafeSerializer

from django.conf import settings

from baserow.core.user.utils import normalize_email_address

from .models import (
    Settings, Group, GroupUser, GroupInvitation, Application,
    GROUP_USER_PERMISSION_CHOICES, GROUP_USER_PERMISSION_ADMIN
)
from .exceptions import (
    GroupDoesNotExist, ApplicationDoesNotExist, BaseURLHostnameNotAllowed,
    GroupInvitationEmailMismatch, GroupInvitationDoesNotExist, GroupUserDoesNotExist,
    GroupUserAlreadyExists, IsNotAdminError
)
from .utils import extract_allowed, set_allowed_attrs
from .registries import application_type_registry
from .signals import (
    application_created, application_updated, application_deleted, group_created,
    group_updated, group_deleted, group_user_updated, group_user_deleted
)
from .emails import GroupInvitationEmail


class CoreHandler:
    def get_settings(self):
        """
        Returns a settings model instance containing all the admin configured settings.

        :return: The settings instance.
        :rtype: Settings
        """

        try:
            return Settings.objects.all()[:1].get()
        except Settings.DoesNotExist:
            return Settings.objects.create()

    def update_settings(self, user, settings_instance=None, **kwargs):
        """
        Updates one or more setting values if the user has staff permissions.

        :param user: The user on whose behalf the settings are updated.
        :type user: User
        :param settings_instance: If already fetched, the settings instance can be
            provided to avoid fetching the values for a second time.
        :type settings_instance: Settings
        :param kwargs: An dict containing the settings that need to be updated.
        :type kwargs: dict
        :return: The update settings instance.
        :rtype: Settings
        """

        if not user.is_staff:
            raise IsNotAdminError(user)

        if not settings_instance:
            settings_instance = self.get_settings()

        for name, value in kwargs.items():
            setattr(settings_instance, name, value)

        settings_instance.save()
        return settings_instance

    def get_group(self, group_id, base_queryset=None):
        """
        Selects a group with a given id from the database.

        :param group_id: The identifier of the group that must be returned.
        :type group_id: int
        :param base_queryset: The base queryset from where to select the group
            object. This can for example be used to do a `prefetch_related`.
        :type base_queryset: Queryset
        :raises GroupDoesNotExist: When the group with the provided id does not exist.
        :return: The requested group instance of the provided id.
        :rtype: Group
        """

        if not base_queryset:
            base_queryset = Group.objects

        try:
            group = base_queryset.get(id=group_id)
        except Group.DoesNotExist:
            raise GroupDoesNotExist(f'The group with id {group_id} does not exist.')

        return group

    def create_group(self, user, **kwargs):
        """
        Creates a new group for an existing user.

        :param user: The user that must be in the group.
        :type user: User
        :return: The newly created GroupUser object
        :rtype: GroupUser
        """

        group_values = extract_allowed(kwargs, ['name'])
        group = Group.objects.create(**group_values)
        last_order = GroupUser.get_last_order(user)
        group_user = GroupUser.objects.create(
            group=group,
            user=user,
            order=last_order,
            permissions=GROUP_USER_PERMISSION_ADMIN
        )

        group_created.send(self, group=group, user=user)

        return group_user

    def update_group(self, user, group, **kwargs):
        """
        Updates the values of a group if the user on whose behalf the request is made
        has admin permissions to the group.

        :param user: The user on whose behalf the change is made.
        :type user: User
        :param group: The group instance that must be updated.
        :type group: Group
        :raises ValueError: If one of the provided parameters is invalid.
        :return: The updated group
        :rtype: Group
        """

        if not isinstance(group, Group):
            raise ValueError('The group is not an instance of Group.')

        group.has_user(user, 'ADMIN', raise_error=True)
        group = set_allowed_attrs(kwargs, ['name'], group)
        group.save()

        group_updated.send(self, group=group, user=user)

        return group

    def delete_group(self, user, group):
        """
        Deletes an existing group and related applications if the user has admin
        permissions to the group.

        :param user: The user on whose behalf the delete is done.
        :type: user: User
        :param group: The group instance that must be deleted.
        :type: group: Group
        :raises ValueError: If one of the provided parameters is invalid.
        """

        if not isinstance(group, Group):
            raise ValueError('The group is not an instance of Group.')

        group.has_user(user, 'ADMIN', raise_error=True)

        # Load the group users before the group is deleted so that we can pass those
        # along with the signal.
        group_id = group.id
        group_users = list(group.users.all())

        # Select all the applications so we can delete them via the handler which is
        # needed in order to call the pre_delete method for each application.
        applications = group.application_set.all().select_related('group')
        for application in applications:
            self.delete_application(user, application)

        group.delete()

        group_deleted.send(self, group_id=group_id, group=group,
                           group_users=group_users, user=user)

    def order_groups(self, user, group_ids):
        """
        Changes the order of groups for a user.

        :param user: The user on whose behalf the ordering is done.
        :type: user: User
        :param group_ids: A list of group ids ordered the way they need to be ordered.
        :type group_ids: List[int]
        """

        for index, group_id in enumerate(group_ids):
            GroupUser.objects.filter(
                user=user,
                group_id=group_id
            ).update(order=index + 1)

    def get_group_user(self, group_user_id, base_queryset=None):
        """
        Fetches a group user object related to the provided id from the database.

        :param group_user_id: The identifier of the group user that must be returned.
        :type group_user_id: int
        :param base_queryset: The base queryset from where to select the group user
            object. This can for example be used to do a `select_related`.
        :type base_queryset: Queryset
        :raises GroupDoesNotExist: When the group with the provided id does not exist.
        :return: The requested group user instance of the provided group_id.
        :rtype: GroupUser
        """

        if not base_queryset:
            base_queryset = GroupUser.objects

        try:
            group_user = base_queryset.select_related('group').get(id=group_user_id)
        except GroupUser.DoesNotExist:
            raise GroupUserDoesNotExist(f'The group user with id {group_user_id} does '
                                        f'not exist.')

        return group_user

    def update_group_user(self, user, group_user, **kwargs):
        """
        Updates the values of an existing group user.

        :param user: The user on whose behalf the group user is deleted.
        :type user: User
        :param group_user: The group user that must be updated.
        :type group_user: GroupUser
        :return: The updated group user instance.
        :rtype: GroupUser
        """

        if not isinstance(group_user, GroupUser):
            raise ValueError('The group user is not an instance of GroupUser.')

        group_user.group.has_user(user, 'ADMIN', raise_error=True)
        group_user = set_allowed_attrs(kwargs, ['permissions'], group_user)
        group_user.save()

        group_user_updated.send(self, group_user=group_user, user=user)

        return group_user

    def delete_group_user(self, user, group_user):
        """
        Deletes the provided group user.

        :param user: The user on whose behalf the group user is deleted.
        :type user: User
        :param group_user: The group user that must be deleted.
        :type group_user: GroupUser
        """

        if not isinstance(group_user, GroupUser):
            raise ValueError('The group user is not an instance of GroupUser.')

        group_user.group.has_user(user, 'ADMIN', raise_error=True)
        group_user_id = group_user.id
        group_user.delete()

        group_user_deleted.send(self, group_user_id=group_user_id,
                                group_user=group_user, user=user)

    def get_group_invitation_signer(self):
        """
        Returns the group invitation signer. This is for example used to create a url
        safe signed version of the invitation id which is used when sending a public
        accept link to the user.

        :return: The itsdangerous serializer.
        :rtype: URLSafeSerializer
        """

        return URLSafeSerializer(settings.SECRET_KEY, 'group-invite')

    def send_group_invitation_email(self, invitation, base_url):
        """
        Sends out a group invitation email to the user based on the provided
        invitation instance.

        :param invitation: The invitation instance for which the email must be send.
        :type invitation: GroupInvitation
        :param base_url: The base url of the frontend, where the user can accept his
            invitation. The signed invitation id is appended to the URL (base_url +
            '/TOKEN'). Only the PUBLIC_WEB_FRONTEND_HOSTNAME is allowed as domain name.
        :type base_url: str
        :raises BaseURLHostnameNotAllowed: When the host name of the base_url is not
            allowed.
        """

        parsed_base_url = urlparse(base_url)
        if parsed_base_url.hostname != settings.PUBLIC_WEB_FRONTEND_HOSTNAME:
            raise BaseURLHostnameNotAllowed(
                f'The hostname {parsed_base_url.netloc} is not allowed.'
            )

        signer = self.get_group_invitation_signer()
        signed_invitation_id = signer.dumps(invitation.id)

        if not base_url.endswith('/'):
            base_url += '/'

        public_accept_url = urljoin(base_url, signed_invitation_id)

        email = GroupInvitationEmail(
            invitation,
            public_accept_url,
            to=[invitation.email]
        )
        email.send()

    def get_group_invitation_by_token(self, token, base_queryset=None):
        """
        Returns the group invitation instance if a valid signed token of the id is
        provided. It can be signed using the signer returned by the
        `get_group_invitation_signer` method.

        :param token: The signed invitation id of related to the group invitation
            that must be fetched. Must be signed using the signer returned by the
            `get_group_invitation_signer`.
        :type token: str
        :param base_queryset: The base queryset from where to select the invitation.
            This can for example be used to do a `select_related`.
        :type base_queryset: Queryset
        :raises BadSignature: When the provided token has a bad signature.
        :raises GroupInvitationDoesNotExist: If the invitation does not exist.
        :return: The requested group invitation instance related to the provided token.
        :rtype: GroupInvitation
        """

        signer = self.get_group_invitation_signer()
        group_invitation_id = signer.loads(token)

        if not base_queryset:
            base_queryset = GroupInvitation.objects

        try:
            group_invitation = base_queryset.select_related(
                'group', 'invited_by'
            ).get(id=group_invitation_id)
        except GroupInvitation.DoesNotExist:
            raise GroupInvitationDoesNotExist(
                f'The group invitation with id {group_invitation_id} does not exist.'
            )

        return group_invitation

    def get_group_invitation(self, group_invitation_id, base_queryset=None):
        """
        Selects a group invitation with a given id from the database.

        :param group_invitation_id: The identifier of the invitation that must be
            returned.
        :type group_invitation_id: int
        :param base_queryset: The base queryset from where to select the invitation.
            This can for example be used to do a `select_related`.
        :type base_queryset: Queryset
        :raises GroupInvitationDoesNotExist: If the invitation does not exist.
        :return: The requested field instance of the provided id.
        :rtype: GroupInvitation
        """

        if not base_queryset:
            base_queryset = GroupInvitation.objects

        try:
            group_invitation = base_queryset.select_related('group', 'invited_by').get(
                id=group_invitation_id
            )
        except GroupInvitation.DoesNotExist:
            raise GroupInvitationDoesNotExist(
                f'The group invitation with id {group_invitation_id} does not exist.'
            )

        return group_invitation

    def create_group_invitation(self, user, group, email, permissions, message,
                                base_url):
        """
        Creates a new group invitation for the given email address and sends out an
        email containing the invitation.

        :param user: The user on whose behalf the invitation is created.
        :type user: User
        :param group: The group for which the user is invited.
        :type group: Group
        :param email: The email address of the person that is invited to the group.
            Can be an existing or not existing user.
        :type email: str
        :param permissions: The group permissions that the user will get once he has
            accepted the invitation.
        :type permissions: str
        :param message: A custom message that will be included in the invitation email.
        :type message: str
        :param base_url: The base url of the frontend, where the user can accept his
            invitation. The signed invitation id is appended to the URL (base_url +
            '/TOKEN'). Only the PUBLIC_WEB_FRONTEND_HOSTNAME is allowed as domain name.
        :type base_url: str
        :raises ValueError: If the provided permissions are not allowed.
        :raises UserInvalidGroupPermissionsError: If the user does not belong to the
            group or doesn't have right permissions in the group.
        :return: The created group invitation.
        :rtype: GroupInvitation
        """

        group.has_user(user, 'ADMIN', raise_error=True)

        if permissions not in dict(GROUP_USER_PERMISSION_CHOICES):
            raise ValueError('Incorrect permissions provided.')

        email = normalize_email_address(email)

        if GroupUser.objects.filter(group=group, user__email=email).exists():
            raise GroupUserAlreadyExists(f'The user {email} is already part of the '
                                         f'group.')

        invitation, created = GroupInvitation.objects.update_or_create(
            group=group,
            email=email,
            defaults={
                'message': message,
                'permissions': permissions,
                'invited_by': user
            }
        )

        self.send_group_invitation_email(invitation, base_url)

        return invitation

    def update_group_invitation(self, user, invitation, permissions):
        """
        Updates the permissions of an existing invitation if the user has ADMIN
        permissions to the related group.

        :param user: The user on whose behalf the invitation is updated.
        :type user: User
        :param invitation: The invitation that must be updated.
        :type invitation: GroupInvitation
        :param permissions: The new permissions of the invitation that the user must
            has after accepting.
        :type permissions: str
        :raises ValueError: If the provided permissions is not allowed.
        :raises UserInvalidGroupPermissionsError: If the user does not belong to the
            group or doesn't have right permissions in the group.
        :return: The updated group permissions instance.
        :rtype: GroupInvitation
        """

        invitation.group.has_user(user, 'ADMIN', raise_error=True)

        if permissions not in dict(GROUP_USER_PERMISSION_CHOICES):
            raise ValueError('Incorrect permissions provided.')

        invitation.permissions = permissions
        invitation.save()

        return invitation

    def delete_group_invitation(self, user, invitation):
        """
        Deletes an existing group invitation if the user has ADMIN permissions to the
        related group.

        :param user: The user on whose behalf the invitation is deleted.
        :type user: User
        :param invitation: The invitation that must be deleted.
        :type invitation: GroupInvitation
        :raises UserInvalidGroupPermissionsError: If the user does not belong to the
            group or doesn't have right permissions in the group.
        """

        invitation.group.has_user(user, 'ADMIN', raise_error=True)
        invitation.delete()

    def reject_group_invitation(self, user, invitation):
        """
        Rejects a group invitation by deleting the invitation so that can't be reused
        again. It can only be rejected if the invitation was addressed to the email
        address of the user.

        :param user: The user who wants to reject the invitation.
        :type user: User
        :param invitation: The invitation that must be rejected.
        :type invitation: GroupInvitation
        :raises GroupInvitationEmailMismatch: If the invitation email does not match
            the one of the user.
        """

        if user.username != invitation.email:
            raise GroupInvitationEmailMismatch(
                'The email address of the invitation does not match the one of the '
                'user.'
            )

        invitation.delete()

    def accept_group_invitation(self, user, invitation):
        """
        Accepts a group invitation by adding the user to the correct group with the
        right permissions. It can only be accepted if the invitation was addressed to
        the email address of the user. Because the invitation has been accepted it
        can then be deleted. If the user is already a member of the group then the
        permissions are updated.

        :param user: The user who has accepted the invitation.
        :type: user: User
        :param invitation: The invitation that must be accepted.
        :type invitation: GroupInvitation
        :raises GroupInvitationEmailMismatch: If the invitation email does not match
            the one of the user.
        :return: The group user relationship related to the invite.
        :rtype: GroupUser
        """

        if user.username != invitation.email:
            raise GroupInvitationEmailMismatch(
                'The email address of the invitation does not match the one of the '
                'user.'
            )

        group_user, created = GroupUser.objects.update_or_create(
            user=user,
            group=invitation.group,
            defaults={
                'order': GroupUser.get_last_order(user),
                'permissions': invitation.permissions
            }
        )
        invitation.delete()

        return group_user

    def get_application(self, application_id, base_queryset=None):
        """
        Selects an application with a given id from the database.

        :param user: The user on whose behalf the application is requested.
        :type user: User
        :param application_id: The identifier of the application that must be returned.
        :type application_id: int
        :param base_queryset: The base queryset from where to select the application
            object. This can for example be used to do a `select_related`.
        :type base_queryset: Queryset
        :raises ApplicationDoesNotExist: When the application with the provided id
            does not exist.
        :return: The requested application instance of the provided id.
        :rtype: Application
        """

        if not base_queryset:
            base_queryset = Application.objects

        try:
            application = base_queryset.select_related(
                'group', 'content_type'
            ).get(id=application_id)
        except Application.DoesNotExist:
            raise ApplicationDoesNotExist(
                f'The application with id {application_id} does not exist.'
            )

        return application

    def create_application(self, user, group, type_name, **kwargs):
        """
        Creates a new application based on the provided type.

        :param user: The user on whose behalf the application is created.
        :type user: User
        :param group: The group that the application instance belongs to.
        :type group: Group
        :param type_name: The type name of the application. ApplicationType can be
            registered via the ApplicationTypeRegistry.
        :type type_name: str
        :param kwargs: The fields that need to be set upon creation.
        :type kwargs: object
        :return: The created application instance.
        :rtype: Application
        """

        group.has_user(user,  raise_error=True)

        # Figure out which model is used for the given application type.
        application_type = application_type_registry.get(type_name)
        model = application_type.model_class
        application_values = extract_allowed(kwargs, ['name'])
        last_order = model.get_last_order(group)

        instance = model.objects.create(group=group, order=last_order,
                                        **application_values)

        application_created.send(self, application=instance, user=user,
                                 type_name=type_name)

        return instance

    def update_application(self, user, application, **kwargs):
        """
        Updates an existing application instance.

        :param user: The user on whose behalf the application is updated.
        :type user: User
        :param application: The application instance that needs to be updated.
        :type application: Application
        :param kwargs: The fields that need to be updated.
        :type kwargs: object
        :raises ValueError: If one of the provided parameters is invalid.
        :return: The updated application instance.
        :rtype: Application
        """

        if not isinstance(application, Application):
            raise ValueError('The application is not an instance of Application.')

        application.group.has_user(user, raise_error=True)

        application = set_allowed_attrs(kwargs, ['name'], application)
        application.save()

        application_updated.send(self, application=application, user=user)

        return application

    def delete_application(self, user, application):
        """
        Deletes an existing application instance.

        :param user: The user on whose behalf the application is deleted.
        :type user: User
        :param application: The application instance that needs to be deleted.
        :type application: Application
        :raises ValueError: If one of the provided parameters is invalid.
        """

        if not isinstance(application, Application):
            raise ValueError('The application is not an instance of Application')

        application.group.has_user(user, raise_error=True)

        application_id = application.id
        application = application.specific
        application_type = application_type_registry.get_by_model(application)
        application_type.pre_delete(user, application)

        application.delete()

        application_deleted.send(self, application_id=application_id,
                                 application=application, user=user)
