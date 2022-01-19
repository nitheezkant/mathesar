import warnings
from psycopg2.errors import DuplicateColumn, UndefinedFunction
from rest_framework import status, viewsets
from rest_framework.exceptions import NotFound, ValidationError, APIException
from rest_framework.response import Response
from sqlalchemy.exc import ProgrammingError

from db.columns.exceptions import (
    DynamicDefaultWarning, InvalidDefaultError, InvalidTypeOptionError, InvalidTypeError
)
from db.columns.operations.select import get_columns_attnum_from_names
from mathesar.api.exceptions import exceptions
from mathesar.api.pagination import DefaultLimitOffsetPagination
from mathesar.api.serializers.columns import ColumnSerializer
from mathesar.api.utils import get_table_or_404
from mathesar.models import Column


class ColumnViewSet(viewsets.ModelViewSet):
    serializer_class = ColumnSerializer
    pagination_class = DefaultLimitOffsetPagination

    def get_queryset(self):
        table = get_table_or_404(pk=self.kwargs['table_pk'])
        sa_column_name = [column.name for column in table.sa_columns]
        column_attnum_list = [result[0] for result in
                              get_columns_attnum_from_names(table.oid, sa_column_name, table.schema._sa_engine)]
        return Column.objects.filter(table=table, attnum__in=column_attnum_list).order_by("attnum")

    def create(self, request, table_pk=None):
        table = get_table_or_404(table_pk)
        # We only support adding a single column through the API.
        serializer = ColumnSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)

        if 'source_column' in serializer.validated_data:
            try:
                column = table.duplicate_column(
                    serializer.validated_data['source_column'],
                    serializer.validated_data['copy_source_data'],
                    serializer.validated_data['copy_source_constraints'],
                    serializer.validated_data.get('name'),
                )
            except IndexError as e:
                _col_idx = serializer.validated_data['source_column']
                raise exceptions.NotFoundAPIError(e,
                                                  message=f'column index "{_col_idx}" not found',
                                                  field='source_column',
                                                  status_code=status.HTTP_400_BAD_REQUEST)
        else:
            try:
                column = table.add_column(request.data)
            except ProgrammingError as e:
                if type(e.orig) == DuplicateColumn:
                    name = request.data['name']
                    raise exceptions.DuplicateTableAPIError(e,
                                                            message=f'Column {name} already exists',
                                                            field='name',
                                                            status_code=status.HTTP_400_BAD_REQUEST)
                else:
                    raise exceptions.ProgrammingAPIError(e)
            except TypeError as e:
                raise exceptions.TypeErrorAPIError(e,
                                                   message="Unknown type_option passed",
                                                   status_code=status.HTTP_400_BAD_REQUEST)
            except InvalidDefaultError as e:
                raise exceptions.InvalidDefaultAPIError(e,
                                                        message=f'default "{request.data["default"]}" is'
                                                                    f' invalid for type {request.data["type"]}',
                                                        status_code=status.HTTP_400_BAD_REQUEST)
            except InvalidTypeOptionError as e:
                type_options = request.data.get('type_options', '')
                raise exceptions.InvalidTypeOptionAPIError(e,
                                                           message=f'parameter dict {type_options} is'
                                                                       f' invalid for type {request.data["type"]}',
                                                           field="type_options",
                                                           status_code=status.HTTP_400_BAD_REQUEST)
            except InvalidTypeError as e:
                raise exceptions.InvalidTypeCastAPIError(e, message='This type casting is invalid.',
                                                         status_code=status.HTTP_400_BAD_REQUEST)
        dj_column = Column(table=table,
                           attnum=get_columns_attnum_from_names(table.oid, [column.name], table.schema._sa_engine)[0][
                               0],
                           **serializer.validated_model_fields)
        dj_column.save()
        out_serializer = ColumnSerializer(dj_column)
        return Response(out_serializer.data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None, table_pk=None):
        column_instance = self.get_object()
        table = column_instance.table
        serializer = ColumnSerializer(instance=column_instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with warnings.catch_warnings():
            warnings.filterwarnings("error", category=DynamicDefaultWarning)
            try:
                table.alter_column(column_instance._sa_column.column_index, serializer.validated_data)
            except ProgrammingError as e:
                if type(e.orig) == UndefinedFunction:
                    raise exceptions.UndefinedFunctionAPIError(e,
                                                               message='This type cast is not implemented',
                                                               status_code=status.HTTP_400_BAD_REQUEST)
                else:
                    raise exceptions.ProgrammingAPIError(e, status_code=status.HTTP_400_BAD_REQUEST)
            except IndexError as e:
                raise exceptions.NotFoundAPIError(e)
            except TypeError as e:
                raise exceptions.InvalidTypeOptionAPIError(e,
                                                           message="Unknown type_option passed",
                                                           status_code=status.HTTP_400_BAD_REQUEST)
            except InvalidDefaultError as e:
                raise exceptions.InvalidDefaultAPIError(e,
                                                        message=f'default "{request.data["default"]}" is'
                                                                    f' invalid for this column',
                                                        status_code=status.HTTP_400_BAD_REQUEST
                                                        )
            except DynamicDefaultWarning as e:
                raise exceptions.DynamicDefaultAPIError(e,
                                                        message='Changing type of columns with dynamically-generated'
                                                                    ' defaults is not supported.'
                                                                    ' Delete or change the default first.',
                                                        status_code=status.HTTP_400_BAD_REQUEST
                                                        )
            except InvalidTypeOptionError as e:
                type_options = request.data.get('type_options', '')
                raise exceptions.InvalidTypeOptionAPIError(e,
                                                           message=f'parameter dict {type_options} is'
                                                                       f' invalid for type {request.data["type"]}',
                                                           status_code=status.HTTP_400_BAD_REQUEST
                                                           )
            except InvalidTypeError as e:
                raise exceptions.InvalidTypeCastAPIError(e,
                                                         message='This type casting is invalid.',
                                                         status_code=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                raise exceptions.APIError(e)
        serializer.update(column_instance, serializer.validated_model_fields)
        # Invalidate the cache as the underlying columns have changed
        out_serializer = ColumnSerializer(self.get_object())
        return Response(out_serializer.data)

    def destroy(self, request, pk=None, table_pk=None):
        column_instance = self.get_object()
        table = column_instance.table
        try:
            table.drop_column(column_instance.column_index)
            column_instance.delete()
        except IndexError:
            raise NotFound
        return Response(status=status.HTTP_204_NO_CONTENT)
