import warnings
from collections import Counter, defaultdict
from functools import reduce
from itertools import chain
from operator import or_
from typing import Dict, List, Optional, Tuple

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.db import models, router
from django.db.models import F
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Cast
from django.utils import timezone

BATCH_SIZE = 1000

from .config import (
    DELETED_BY_CASCADE_FIELD_NAME,
    FIELD_NAME,
    HARD_DELETE,
    HARD_DELETE_NOCASCADE,
    NO_DELETE,
    SOFT_DELETE,
    SOFT_DELETE_CASCADE,
    IS_DELETED_FIELD_NAME,
)
from .managers import (
    SafeDeleteAllManager,
    SafeDeleteDeletedManager,
    SafeDeleteManager,
)
from .signals import post_softdelete, post_undelete, pre_softdelete
from .utils import SafeDeleteCollector, can_hard_delete, related_objects


def is_safedelete_cls(cls):
    for base in cls.__bases__:
        # This used to check if it startswith 'safedelete', but that masks
        # the issue inside of a test. Other clients create models that are
        # outside of the safedelete package.
        if base.__module__.startswith('django_safedelete_pro.models'):
            return True
        if is_safedelete_cls(base):
            return True
    return False


def is_safedelete(related):
    warnings.warn(
        'is_safedelete is deprecated in favor of is_safedelete_cls',
        DeprecationWarning)
    return is_safedelete_cls(related.__class__)


class SafeDeleteModel(models.Model):
    """Abstract safedelete-ready model.

    .. note::
        To create your safedelete-ready models, you have to make them inherit from this model.

    :attribute deleted:
        DateTimeField set to the moment the object was deleted. Is set to
        ``None`` if the object has not been deleted.

    :attribute deleted_by_cascade:
        BooleanField set True whenever the object is deleted due cascade operation called by delete
        method of any parent Model. Default value is False. Later if its parent model calls for
        cascading undelete, it will restore only child classes that were also deleted by a cascading
        operation (deleted_by_cascade equals to True), i.e. all objects that were deleted before their
        parent deletion, should keep deleted if the same parent object is restored by undelete method.

        If this behavior isn't desired, class that inherits from SafeDeleteModel can override this
        attribute by setting it as None: overriding model class won't have its ``deleted_by_cascade``
        field and won't be restored by cascading undelete even if it was deleted by a cascade operation.

        >>> class MyModel(SafeDeleteModel):
        ...     deleted_by_cascade = None
        ...     my_field = models.TextField()

    :attribute _safedelete_policy: define what happens when you delete an object.
        It can be one of ``HARD_DELETE``, ``SOFT_DELETE``, ``SOFT_DELETE_CASCADE``, ``NO_DELETE`` and ``HARD_DELETE_NOCASCADE``.
        Defaults to ``SOFT_DELETE``.

        >>> class MyModel(SafeDeleteModel):
        ...     _safedelete_policy = SOFT_DELETE
        ...     my_field = models.TextField()
        ...
        >>> # Now you have your model (with its ``deleted`` field, and custom manager and delete method)

    :attribute objects:
        The :class:`safedelete.managers.SafeDeleteManager` returns the non-deleted models.

    :attribute all_objects:
        The :class:`safedelete.managers.SafeDeleteAllManager` returns all the models (non-deleted and soft-deleted).

    :attribute deleted_objects:
        The :class:`safedelete.managers.SafeDeleteDeletedManager` returns the soft-deleted models.
    """

    _safedelete_policy: int = SOFT_DELETE_CASCADE

    deleted_at = models.DateTimeField(editable=False, null=True, db_index=True, verbose_name='删除时间')
    is_deleted = models.CharField(
        editable=False, default='-1', db_default='-1', max_length=36, db_index=True, verbose_name='删除状态'
    )
    deleted_by_cascade = models.BooleanField(
        editable=False, default=False, db_default=False, verbose_name='是否被级联删除',
    )

    if FIELD_NAME != 'deleted_at':
        vars()[FIELD_NAME] = deleted_at
        del deleted_at
    if IS_DELETED_FIELD_NAME != 'is_deleted':
        vars()[IS_DELETED_FIELD_NAME] = is_deleted
        del is_deleted
    if DELETED_BY_CASCADE_FIELD_NAME != 'deleted_by_cascade':
        vars()[DELETED_BY_CASCADE_FIELD_NAME] = deleted_by_cascade
        del deleted_by_cascade

    objects = SafeDeleteManager()
    all_objects = SafeDeleteAllManager()
    deleted_objects = SafeDeleteDeletedManager()

    class Meta:
        abstract = True

    @classmethod
    def _has_field(cls, field_name: str) -> bool:
        return any(f.name == field_name for f in cls._meta.local_fields)

    def save(self, keep_deleted=False, **kwargs):
        """Save an object, un-deleting it if it was deleted.

        Args:
            keep_deleted: Do not undelete the model if soft-deleted. (default: {False})
            kwargs: Passed onto :func:`save`.

        .. note::
            Undeletes soft-deleted models by default.
        """
        was_undeleted = False
        if not keep_deleted:
            if getattr(self, FIELD_NAME) and self.pk:
                was_undeleted = True
            setattr(self, FIELD_NAME, None)
            if self._has_field(DELETED_BY_CASCADE_FIELD_NAME):
                setattr(self, DELETED_BY_CASCADE_FIELD_NAME, False)
            if self._has_field(IS_DELETED_FIELD_NAME):
                setattr(self, IS_DELETED_FIELD_NAME, '-1')

        super(SafeDeleteModel, self).save(**kwargs)

        if was_undeleted:
            # send undelete signal
            using = kwargs.get('using') or router.db_for_write(self.__class__, instance=self)
            post_undelete.send(sender=self.__class__, instance=self, using=using)

    async def asave(self, keep_deleted=False, **kwargs):
        return await sync_to_async(self.save)(keep_deleted=keep_deleted, **kwargs)

    @classmethod
    def _get_unique_field_groups(cls) -> List[Tuple[str, ...]]:
        groups: List[Tuple[str, ...]] = [
            (f.attname,) for f in cls._meta.fields if f._unique and not f.primary_key  # type: ignore
        ]
        for ut in cls._meta.unique_together:
            groups.append(tuple(cls._meta.get_field(f).attname for f in ut))  # type: ignore
        for c in cls._meta.total_unique_constraints:
            if c.fields:
                groups.append(tuple(cls._meta.get_field(f).attname for f in c.fields))  # type: ignore
        return groups

    @classmethod
    def _deduplicate_by_unique_latest(cls, instances: List[models.Model]) -> List[models.Model]:
        if len(instances) <= 1 or not is_safedelete_cls(cls):
            return instances
        groups = cls._get_unique_field_groups()
        if not groups:
            return instances
        sorted_objs = sorted(
            instances,
            key=lambda o: (getattr(o, FIELD_NAME) is not None, getattr(o, FIELD_NAME)),
            reverse=True,
        )
        undeleted_overrides = {
            FIELD_NAME: None,
            IS_DELETED_FIELD_NAME: '-1',
            DELETED_BY_CASCADE_FIELD_NAME: False,
        }
        seen = set()
        result = []
        for obj in sorted_objs:
            keys = []
            conflict = False
            for grp in groups:
                vals = tuple(undeleted_overrides.get(f, getattr(obj, f, None)) for f in grp)
                if any(v is None for f, v in zip(grp, vals) if f != FIELD_NAME):
                    continue
                key = (grp, vals)
                if key in seen:
                    conflict = True
                    break
                keys.append(key)
            if not conflict:
                seen.update(keys)
                result.append(obj)
        return result

    @classmethod
    def _bulk_undelete_group(cls, model, instances, using, is_cascade=False, **kwargs) -> Dict[str, int]:
        instances = model._deduplicate_by_unique_latest(instances)
        if not instances:
            return {}
        has_custom_save = (
            model.save is not SafeDeleteModel.save
            or model.undelete is not SafeDeleteModel.undelete
        )
        if has_custom_save or (not is_cascade and len(instances) == 1):
            for obj in instances:
                obj.save(keep_deleted=False, **kwargs)
            return {model._meta.label: len(instances)}

        has_is_del = model._has_field(IS_DELETED_FIELD_NAME)
        has_cascade = model._has_field(DELETED_BY_CASCADE_FIELD_NAME)
        send_post = (
            post_undelete.has_listeners(model)
            or getattr(post_undelete.send, '__self__', None) is not post_undelete
        )
        pks = []
        for obj in instances:
            setattr(obj, FIELD_NAME, None)
            if has_cascade:
                setattr(obj, DELETED_BY_CASCADE_FIELD_NAME, False)
            if has_is_del:
                setattr(obj, IS_DELETED_FIELD_NAME, '-1')
            pks.append(obj.pk)

        update_fields = {FIELD_NAME: None}
        if has_cascade:
            update_fields[DELETED_BY_CASCADE_FIELD_NAME] = False
        if has_is_del:
            update_fields[IS_DELETED_FIELD_NAME] = '-1'
        for i in range(0, len(pks), BATCH_SIZE):
            model.all_objects.using(using).filter(pk__in=pks[i:i + BATCH_SIZE]).update(**update_fields)

        if send_post:
            for obj in instances:
                post_undelete.send(sender=model, instance=obj, using=using)
        return {model._meta.label: len(instances)}

    @classmethod
    def _batch_undelete(cls, objs, force_policy: Optional[int] = None, **kwargs) -> Tuple[int, Dict[str, int]]:
        if not objs:
            return (0, {})
        current_policy = force_policy or cls._safedelete_policy
        using = kwargs.pop('using', None) or router.db_for_write(cls)
        undeleted_counter: Counter = Counter()

        root_deleted = cls._deduplicate_by_unique_latest(
            [o for o in objs if getattr(o, FIELD_NAME)]
        )
        if not root_deleted:
            return (0, {})
        undeleted_counter.update(
            cls._bulk_undelete_group(cls, root_deleted, using, is_cascade=False, **kwargs)
        )

        if current_policy == SOFT_DELETE_CASCADE:
            related_by_model: Dict[type, List[models.Model]] = defaultdict(list)
            seen_related = set()
            for related in related_objects(root_deleted, only_deleted_by_cascade=True):
                if is_safedelete_cls(related.__class__) and getattr(related, FIELD_NAME):
                    key = (related.__class__, related.pk)
                    if key not in seen_related:
                        seen_related.add(key)
                        related_by_model[related.__class__].append(related)
            for rel_model, rel_instances in related_by_model.items():
                undeleted_counter.update(
                    cls._bulk_undelete_group(rel_model, rel_instances, using, is_cascade=True, **kwargs)
                )

        return sum(undeleted_counter.values()), dict(undeleted_counter)

    def undelete(self, force_policy: Optional[int] = None, **kwargs) -> Tuple[int, Dict[str, int]]:
        """Undelete a soft-deleted model.

        Args:
            force_policy: Force a specific undelete policy. (default: {None})
            kwargs: Passed onto :func:`save`.

        .. note::
            Will raise a :class:`AssertionError` if the model was not soft-deleted.
        """
        assert getattr(self, FIELD_NAME)
        return self._batch_undelete([self], force_policy=force_policy, **kwargs)

    async def aundelete(self, force_policy: Optional[int] = None, **kwargs) -> Tuple[int, Dict[str, int]]:
        return await sync_to_async(self.undelete)(force_policy=force_policy, **kwargs)

    def delete(self, force_policy=None, **kwargs):
        # To know why we need to do that, see https://github.com/makinacorpus/django-safedelete/issues/117
        return self._delete(force_policy, **kwargs)

    async def adelete(self, force_policy=None, **kwargs):
        return await sync_to_async(self.delete)(force_policy=force_policy, **kwargs)

    def _delete(self, force_policy: Optional[int] = None, **kwargs) -> Tuple[int, Dict[str, int]]:
        """Overrides Django's delete behaviour based on the model's delete policy.

        Args:
            force_policy: Force a specific delete policy. (default: {None})
            kwargs: Passed onto :func:`save` if soft deleted.
        """
        current_policy = self._safedelete_policy if (force_policy is None) else force_policy
        if current_policy == NO_DELETE:
            return (0, {})
        elif current_policy == SOFT_DELETE:
            return self.soft_delete_policy_action(**kwargs)
        elif current_policy == HARD_DELETE:
            return self.hard_delete_policy_action(**kwargs)
        elif current_policy == HARD_DELETE_NOCASCADE:
            return self.hard_delete_cascade_policy_action(**kwargs)
        elif current_policy == SOFT_DELETE_CASCADE:
            return self.soft_delete_cascade_policy_action(**kwargs)
        return (0, {})

    def soft_delete_policy_action(self, **kwargs) -> Tuple[int, Dict[str, int]]:
        # Only soft-delete the object, marking it as deleted.
        setattr(self, FIELD_NAME, timezone.now())
        if self._has_field(IS_DELETED_FIELD_NAME):
            setattr(self, IS_DELETED_FIELD_NAME, str(self.pk))
        # is_cascade shouldn't be in kwargs when calling save method.
        if kwargs.pop('is_cascade', False) and self._has_field(DELETED_BY_CASCADE_FIELD_NAME):
            setattr(self, DELETED_BY_CASCADE_FIELD_NAME, True)
        using = kwargs.get('using') or router.db_for_write(self.__class__, instance=self)
        # send pre_softdelete signal
        pre_softdelete.send(sender=self.__class__, instance=self, using=using)
        if not self._state.adding and 'update_fields' not in kwargs and not pre_softdelete.has_listeners(self.__class__):
            kwargs['update_fields'] = [
                f for f in (FIELD_NAME, IS_DELETED_FIELD_NAME, DELETED_BY_CASCADE_FIELD_NAME) if self._has_field(f)
            ]
        self.save(keep_deleted=True, **kwargs)
        # send softdelete signal
        post_softdelete.send(sender=self.__class__, instance=self, using=using)

        return (1, {self._meta.label: 1})

    def hard_delete_policy_action(self, **kwargs) -> Tuple[int, Dict[str, int]]:
        # Normally hard-delete the object.
        return super(SafeDeleteModel, self).delete()

    def hard_delete_cascade_policy_action(self, **kwargs) -> Tuple[int, Dict[str, int]]:
        # Hard-delete the object only if nothing would be deleted with it
        if not can_hard_delete(self):
            return self._delete(force_policy=SOFT_DELETE, **kwargs)
        else:
            return self._delete(force_policy=HARD_DELETE, **kwargs)

    @classmethod
    def _bulk_soft_delete_group(cls, model, instances, using, is_cascade=False, **kwargs) -> Dict[str, int]:
        if not instances:
            return {}
        has_custom_delete = (
            model.delete is not SafeDeleteModel.delete
            or model._delete is not SafeDeleteModel._delete
        )
        if has_custom_delete or (not is_cascade and len(instances) == 1):
            counter: Counter = Counter()
            for obj in instances:
                if is_cascade:
                    _, resp = obj.delete(force_policy=SOFT_DELETE, is_cascade=True, **kwargs)
                else:
                    _, resp = obj._delete(force_policy=SOFT_DELETE, **kwargs)
                counter.update(resp)
            return dict(counter)

        now = timezone.now()
        has_is_del = model._has_field(IS_DELETED_FIELD_NAME)
        has_cascade = is_cascade and model._has_field(DELETED_BY_CASCADE_FIELD_NAME)
        send_pre = pre_softdelete.has_listeners(model) or getattr(pre_softdelete.send, '__self__', None) is not pre_softdelete
        send_post = post_softdelete.has_listeners(model) or getattr(post_softdelete.send, '__self__', None) is not post_softdelete

        pks = []
        for obj in instances:
            setattr(obj, FIELD_NAME, now)
            if has_is_del:
                setattr(obj, IS_DELETED_FIELD_NAME, str(obj.pk))
            if has_cascade:
                setattr(obj, DELETED_BY_CASCADE_FIELD_NAME, True)
            pks.append(obj.pk)
            if send_pre:
                pre_softdelete.send(sender=model, instance=obj, using=using)

        update_fields = {FIELD_NAME: now}
        if has_is_del:
            update_fields[IS_DELETED_FIELD_NAME] = Cast(F('pk'), output_field=models.CharField(max_length=36))
        if has_cascade:
            update_fields[DELETED_BY_CASCADE_FIELD_NAME] = True
        for i in range(0, len(pks), BATCH_SIZE):
            model.all_objects.using(using).filter(pk__in=pks[i:i + BATCH_SIZE]).update(**update_fields)

        if send_post:
            for obj in instances:
                post_softdelete.send(sender=model, instance=obj, using=using)
        return {model._meta.label: len(instances)}

    @classmethod
    def _batch_soft_delete_cascade(cls, objs, **kwargs) -> Tuple[int, Dict[str, int]]:
        if not objs:
            return (0, {})
        using = kwargs.pop('using', None) or router.db_for_write(cls)
        collector = SafeDeleteCollector(using=using, exclude_deleted=True)
        collector.collect(objs)
        # Soft-delete-cascade raises an exception when trying to delete an object that related object is PROTECT
        protected_objects = defaultdict(list)
        for obj in collector.protected:
            if getattr(obj, FIELD_NAME, None) is None:
                protected_objects[obj.__class__.__name__].append(obj)
        if protected_objects:
            raise ProtectedError(
                'Cannot delete some instances of model %r because they are '
                'referenced through protected foreign keys: %s.' % (
                    cls.__name__,
                    ', '.join(protected_objects),
                ),
                set(chain.from_iterable(protected_objects.values())),
            )

        # Batch soft-delete on related objects before
        deleted_counter: Counter = Counter()
        related_by_model: Dict[type, List[models.Model]] = defaultdict(list)
        seen_related = set()
        for related in related_objects(objs, collector=collector):
            if is_safedelete_cls(related.__class__) and not getattr(related, FIELD_NAME):
                key = (related.__class__, related.pk)
                if key not in seen_related:
                    seen_related.add(key)
                    related_by_model[related.__class__].append(related)

        for rel_model, rel_instances in related_by_model.items():
            deleted_counter.update(
                cls._bulk_soft_delete_group(rel_model, rel_instances, using, is_cascade=True, **kwargs)
            )

        # Soft-delete root objects
        root_active = [o for o in objs if not getattr(o, FIELD_NAME)]
        deleted_counter.update(
            cls._bulk_soft_delete_group(cls, root_active, using, is_cascade=False, **kwargs)
        )

        # update fields (SET, SET_DEFAULT or SET_NULL)
        for (field, value), instances_list in collector.field_updates.items():
            updates = []
            objs_to_update = []
            for instances in instances_list:
                if isinstance(instances, models.QuerySet) and instances._result_cache is None:
                    updates.append(instances)
                else:
                    objs_to_update.extend(instances)
            if updates:
                combined_updates = reduce(or_, updates)
                combined_updates.update(**{field.name: value})
            if objs_to_update:
                target_model = objs_to_update[0].__class__
                query = models.sql.UpdateQuery(target_model)
                query.update_batch(
                    list({obj.pk for obj in objs_to_update}), {field.name: value}, collector.using
                )

        return sum(deleted_counter.values()), dict(deleted_counter)

    def soft_delete_cascade_policy_action(self, **kwargs) -> Tuple[int, Dict[str, int]]:
        return self._batch_soft_delete_cascade([self], **kwargs)

    @classmethod
    def has_unique_fields(cls) -> bool:
        """Checks if one of the fields of this model has a unique constraint set (unique=True).

        It also checks if the model has sets of field names that, taken together, must be unique.
        """
        if cls._meta.unique_together or cls._meta.total_unique_constraints:
            return True

        for field in cls._meta.fields:
            if field._unique:  # type: ignore
                return True
        return False

    # We need to overwrite this check to ensure uniqueness is also checked
    # against "deleted" (but still in db) objects.
    # FIXME: Better/cleaner way ?
    def _perform_unique_checks(self, unique_checks) -> Dict[str, List[ValidationError]]:
        errors: Dict[str, List[ValidationError]] = {}

        for model_class, unique_check in unique_checks:
            lookup_kwargs = {}
            for field_name in unique_check:
                f = self._meta.get_field(field_name)
                lookup_value = getattr(self, f.attname)  # type: ignore
                if lookup_value is None:
                    continue
                if f.primary_key and not self._state.adding:  # type: ignore
                    continue
                lookup_kwargs[str(field_name)] = lookup_value
            if len(unique_check) != len(lookup_kwargs):
                continue

            # This is the changed line
            if hasattr(model_class, 'all_objects'):
                qs = model_class.all_objects.filter(**lookup_kwargs)
            else:
                qs = model_class._default_manager.filter(**lookup_kwargs)

            model_class_pk = self._get_pk_val(model_class._meta)  # type: ignore
            if not self._state.adding and model_class_pk is not None:
                qs = qs.exclude(pk=model_class_pk)
            if qs.exists():
                if len(unique_check) == 1:
                    key = unique_check[0]
                else:
                    key = models.base.NON_FIELD_ERRORS  # type: ignore
                errors.setdefault(key, []).append(
                    self.unique_error_message(model_class, unique_check)
                )
        return errors


class SafeDeleteMixin(SafeDeleteModel):
    """``SafeDeleteModel`` was previously named ``SafeDeleteMixin``.

    .. deprecated:: 0.4.0
        Use :class:`SafeDeleteModel` instead.
    """

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        warnings.warn('The SafeDeleteMixin class was renamed SafeDeleteModel',
                      DeprecationWarning)
        SafeDeleteModel.__init__(self, *args, **kwargs)
