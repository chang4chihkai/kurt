from construct import *
from functools import partial
import inspect

from inline_objects import field, Ref
from fixed_objects import *
import fixed_objects
from user_objects import *
import user_objects


### DEBUG
class PrintContext(Construct):
    def _parse(self, stream, context):
        print 'parse', context
    
    def _build(self, obj, stream, context):
        print 'build', context
###


class ObjectAdapter(Adapter):
    """Decodes a construct to a pythonic class representation.
    The class must have a from_construct classmethod and a to_construct instancemethod.
    """
    def __init__(self, classes, *args, **kwargs):
        """Initialize an adapter for a new type/object(s).
        @param classes: class, list of classes, or dict of obj.classID name to class mapping.
            eg ObjectAdapter({"String": String, "Array": Collection}, <subcon>)
        Note: Must use new-style objects, ie. subclasses of object.
        """
        Adapter.__init__(self, *args, **kwargs)
        
        if isinstance(classes, list):
            classes = dict((cls.__name__, cls) for cls in classes)
        self.classes = classes
    
    def _get_class(self, classID):
        if inspect.isclass(self.classes):
            return self.classes
        else:
            return self.classes[classID]
    
    def _encode(self, obj, context):
        """Encodes a class to a lower-level object using the class' own to_construct function.
        If no such function is defined, returns the object unchanged.
        """
        func = getattr(obj, 'to_construct', None)
        if callable(func):
            return func(context)
        else:
            return obj
    
    def _decode(self, obj, context):
        """Initialises a new Python class from a construct using the mapping passed to the adapter.
        """
        cls = self._get_class(obj.classID)
        return cls.from_construct(obj, context)


def obj_classes_from_module(module):
    """Return a list of classes in a module that have a 'classID' attribute."""
    for name in dir(module):
        if not name.startswith('_'):
            cls = getattr(module, name)
            if getattr(cls, 'classID', None):
                yield (name, cls)



### Fixed-format objects ###

fixed_object_classes = []
fixed_object_ids_by_name = {}
fixed_object_cons_by_name = {}

for (name, cls) in obj_classes_from_module(fixed_objects):
    fixed_object_classes.append(cls)
    fixed_object_ids_by_name[name] = cls.classID
    fixed_object_cons_by_name[name] = cls._construct

FixedObjectAdapter = partial(ObjectAdapter, fixed_object_classes)

fixed_object = FixedObjectAdapter(Struct("fixed_object",
    Enum(UBInt8("classID"), **fixed_object_ids_by_name),
    Switch("value", lambda ctx: ctx.classID, fixed_object_cons_by_name),
))
fixed_object.__doc__ = """Construct for FixedObjects.
Stored in the object table. May contain references."""


### User-class objects ###

user_object_classes = []
user_object_ids_by_name = {}

for (name, cls) in obj_classes_from_module(user_objects):
    user_object_classes.append(cls)
    user_object_ids_by_name[name] = cls.classID

UserObjectAdapter = partial(ObjectAdapter, user_object_classes)

user_object = UserObjectAdapter(Struct("user_object",
    Enum(UBInt8("classID"),
        **user_object_ids_by_name
    ),
    UBInt8("version"),
    UBInt8("length"),
    Rename("field_values", MetaRepeater(lambda ctx: ctx.length, field)),
))
user_object.__doc__ = """Construct for UserObjects.
Stored in the object table. May contain references."""


### Object Table ###

class ObjectAdapter(Adapter):
    def _encode(self, obj, context):
        classID = obj.classID
        if classID in fixed_object_ids_by_name:
            classID = fixed_object_ids_by_name[classID]
        elif classID in user_object_ids_by_name:
            classID = user_object_ids_by_name[classID]
        return Container(
            classID = classID,
            object = obj,
        )
    
    def _decode(self, obj, context):
        return obj.object

obj_entry = ObjectAdapter(Struct("object",
    Peek(UBInt8("classID")),
    IfThenElse("object", lambda ctx: ctx.classID < 99,
        fixed_object,
        user_object,
    ),
))
obj_entry.__doc__ = """Construct for object table entries, both UserObjects and FixedObjects."""


class ObjectTableAdapter(Adapter):
    def _encode(self, objects, context):
        return Container(
            header = "ObjS\x01Stch\x01",
            length = len(objects),
            objects = objects,
        )
    
    def _decode(self, table, context):
        assert table.length == len(table.objects) # DEBUG
        return table.objects


class ObjectNetworkAdapter(Adapter):
    """Object network <--> object table listing objects containing Refs"""
    def _encode(self, root, context):
        def get_ref(value):            
            """Returns the index of the given object in the object table, adding it if needed."""
            objects = self._objects
            if isinstance(value, UserObject) or isinstance(value, FixedObject): # or isinstance(obj, ContainsRefs):
                # must handle both back and forward refs.
                proc_objects = [getattr(obj, '_made_from', None) for obj in objects]
                
                for i in range(len(objects)):
                    if value is objects[i]:
                        index = i + 1 # first entry's index is 1
                        break
                else:
                    for i in range(len(proc_objects)):
                        if value is proc_objects[i]:
                            index = i + 1
                            break
                    else:
                        objects.append(value)
                        index = len(objects)
                
                return Ref(index)
            else:
                # Inline value
                return value
        
        def fix_fields(obj):
            if isinstance(obj, UserObject):
                field_values = [get_ref(value) for value in obj.field_values]
                fixed_obj = obj.__class__(field_values, version = obj.version)
            
            elif isinstance(obj, Dictionary):
                fixed_obj = obj.__class__(dict((get_ref(field), get_ref(value)) for (field, value) in obj.value.items()))
                
            elif isinstance(obj, Form):
                fixed_obj = obj.__class__(**dict((field, get_ref(value)) for (field, value) in obj.value.items()))
                
            elif isinstance(obj, ContainsRefs):
                fixed_obj = obj.__class__([get_ref(field) for field in obj.value])
                
            else:
                return obj
            
            fixed_obj._made_from = obj
            return fixed_obj
        
        i = 0
        self._objects = objects = [root]
        while i < len(objects):
            objects[i] = fix_fields(objects[i])
            i += 1
        
        return objects
    
    def _decode(self, objects, context):        
        def resolve_ref(obj, objects=objects):
            if isinstance(obj, Ref):
                # first entry is 1
                return objects[obj.index - 1]
            else:
                return obj
        
        for obj in objects:
            if isinstance(obj, UserObject):
                for field_name in obj.fields:
                    value = obj.fields[field_name]
                    value = resolve_ref(value)
                    obj.fields[field_name] = value
            
            elif isinstance(obj, Dictionary):
                obj.value = dict((resolve_ref(field), resolve_ref(value)) for (field, value) in obj.value.items())
            
            elif isinstance(obj, Form):
                for field in obj.value:
                    value = getattr(obj, field)
                    value = resolve_ref(value)
                    setattr(obj, field, value)
            
            elif isinstance(obj, ContainsRefs):
                obj.value = [resolve_ref(field) for field in obj.value]
        
        root = objects[0]
        return root

_obj_table_entries = ObjectTableAdapter(Struct("object_table",
    Const(Bytes("header", 10), "ObjS\x01Stch\x01"),
    UBInt32("length"),
    Rename("objects", MetaRepeater(lambda ctx: ctx.length, obj_entry)),
))

obj_table = ObjectNetworkAdapter(_obj_table_entries)
obj_table.__doc__ = """Construct for parsing a binary object table to pythonic object(s).
Includes "ObjS\\x01Stch\\x01" header.
"""

__all__ = ['obj_table', '_obj_table_entries', 'UserObject', 'FixedObject'] + [cls.__name__ for cls in fixed_object_classes + user_object_classes]
