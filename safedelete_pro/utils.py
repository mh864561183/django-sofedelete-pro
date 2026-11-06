from itertools import chain

from django.contrib.admin.utils import NestedObjects
from django.db import router

from .config import DELETED_BY_CASCADE_FIELD_NAME


def related_objects(obj, only_deleted_by_cascade=False, collector=None):
    """ Return a generator to the objects that would be deleted if we delete "obj" (excluding obj)

    Args:
        only_deleted_by_cascade: Include filter in flatten method to bypass elements controling undelete cascading.
        collector: Optional pre-collected NestedObjects instance to avoid duplicate DB queries.
    """
    objs = list(obj) if isinstance(obj, (list, tuple, set)) else [obj]
    if not objs:
        return iter(())
    root_set = set(objs)

    if collector is None:
        collector = NestedObjects(using=router.db_for_write(type(objs[0])))
        collector.collect(objs)

    def flatten(elem):
        if isinstance(elem, tuple):
            return elem
        elif elem in root_set or not only_deleted_by_cascade or getattr(elem, DELETED_BY_CASCADE_FIELD_NAME, False):
            elem = [(elem,) if elem not in root_set else (), *collector.edges.get(elem, [])]
            return chain.from_iterable(map(flatten, elem))
        return ()

    return chain.from_iterable(map(flatten, collector.edges.get(None, ())))


def can_hard_delete(obj):
    return not bool(list(related_objects(obj)))
