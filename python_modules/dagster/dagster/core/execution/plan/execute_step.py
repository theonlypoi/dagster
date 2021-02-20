from collections import defaultdict
from typing import Any, Dict, Iterator, List, Optional, Set, Union

from dagster import check
from dagster.core.definitions import (
    AssetKey,
    AssetMaterialization,
    ExpectationResult,
    Failure,
    Materialization,
    Output,
    OutputDefinition,
    RetryRequested,
    TypeCheck,
)
from dagster.core.definitions.events import (
    AssetPartitions,
    DynamicOutput,
    EventMetadataEntry,
    PartitionSpecificMetadataEntry,
)
from dagster.core.errors import (
    DagsterExecutionHandleOutputError,
    DagsterExecutionStepExecutionError,
    DagsterInvariantViolationError,
    DagsterStepOutputNotFoundError,
    DagsterTypeCheckDidNotPass,
    DagsterTypeCheckError,
    DagsterTypeMaterializationError,
    user_code_error_boundary,
)
from dagster.core.events import DagsterEvent
from dagster.core.execution.context.system import (
    OutputContext,
    SystemStepExecutionContext,
    TypeCheckContext,
)
from dagster.core.execution.plan.compute import execute_core_compute
from dagster.core.execution.plan.inputs import StepInputData
from dagster.core.execution.plan.objects import StepSuccessData, TypeCheckData
from dagster.core.execution.plan.outputs import StepOutputData, StepOutputHandle
from dagster.core.storage.intermediate_storage import IntermediateStorageAdapter
from dagster.core.storage.io_manager import IOManager
from dagster.core.storage.tags import MEMOIZED_RUN_TAG
from dagster.core.types.dagster_type import DagsterType, DagsterTypeKind
from dagster.utils import ensure_gen, iterate_with_context
from dagster.utils.timing import time_execution_scope

from .compute import SolidOutputUnion


def _step_output_error_checked_user_event_sequence(
    step_context: SystemStepExecutionContext, user_event_sequence: Iterator[SolidOutputUnion]
) -> Iterator[SolidOutputUnion]:
    """
    Process the event sequence to check for invariant violations in the event
    sequence related to Output events emitted from the compute_fn.

    This consumes and emits an event sequence.
    """
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.generator_param(user_event_sequence, "user_event_sequence")

    step = step_context.step
    output_names = list([output_def.name for output_def in step.step_outputs])
    seen_outputs: Set[str] = set()
    seen_mapping_keys: Dict[str, Set[str]] = defaultdict(set)

    for user_event in user_event_sequence:
        if not isinstance(user_event, (Output, DynamicOutput)):
            yield user_event
            continue

        # do additional processing on Outputs
        output = user_event
        if not step.has_step_output(output.output_name):
            raise DagsterInvariantViolationError(
                'Core compute for solid "{handle}" returned an output '
                '"{output.output_name}" that does not exist. The available '
                "outputs are {output_names}".format(
                    handle=str(step.solid_handle), output=output, output_names=output_names
                )
            )

        step_output = step.step_output_named(output.output_name)
        output_def = step_context.pipeline_def.get_solid(step_output.solid_handle).output_def_named(
            step_output.name
        )

        if isinstance(output, Output):
            if output.output_name in seen_outputs:
                raise DagsterInvariantViolationError(
                    'Compute for solid "{handle}" returned an output '
                    '"{output.output_name}" multiple times'.format(
                        handle=str(step.solid_handle), output=output
                    )
                )

            if output_def.is_dynamic:
                raise DagsterInvariantViolationError(
                    f'Compute for solid "{step.solid_handle}" for output "{output.output_name}" '
                    "defined as dynamic must yield DynamicOutput, got Output."
                )
        else:
            if not output_def.is_dynamic:
                raise DagsterInvariantViolationError(
                    f'Compute for solid "{step.solid_handle}" yielded a DynamicOutput, '
                    "but did not use DynamicOutputDefinition."
                )
            if output.mapping_key in seen_mapping_keys[output.output_name]:
                raise DagsterInvariantViolationError(
                    f'Compute for solid "{step.solid_handle}" yielded a DynamicOutput with '
                    f'mapping_key "{output.mapping_key}" multiple times.'
                )
            seen_mapping_keys[output.output_name].add(output.mapping_key)

        yield output
        seen_outputs.add(output.output_name)

    for step_output in step.step_outputs:
        step_output_def = step_context.solid_def.output_def_named(step_output.name)
        if not step_output_def.name in seen_outputs and not step_output_def.optional:
            if step_output_def.dagster_type.kind == DagsterTypeKind.NOTHING:
                step_context.log.info(
                    'Emitting implicit Nothing for output "{output}" on solid {solid}'.format(
                        output=step_output_def.name, solid={str(step.solid_handle)}
                    )
                )
                yield Output(output_name=step_output_def.name, value=None)
            else:
                raise DagsterStepOutputNotFoundError(
                    'Core compute for solid "{handle}" did not return an output '
                    'for non-optional output "{step_output_def.name}"'.format(
                        handle=str(step.solid_handle), step_output_def=step_output_def
                    ),
                    step_key=step.key,
                    output_name=step_output_def.name,
                )


def _do_type_check(context: TypeCheckContext, dagster_type: DagsterType, value: Any) -> TypeCheck:
    type_check = dagster_type.type_check(context, value)
    if not isinstance(type_check, TypeCheck):
        return TypeCheck(
            success=False,
            description=(
                "Type checks must return TypeCheck. Type check for type {type_name} returned "
                "value of type {return_type} when checking runtime value of type {dagster_type}."
            ).format(
                type_name=dagster_type.display_name,
                return_type=type(type_check),
                dagster_type=type(value),
            ),
        )
    return type_check


def _create_step_input_event(
    step_context: SystemStepExecutionContext, input_name: str, type_check: TypeCheck, success: bool
) -> DagsterEvent:
    return DagsterEvent.step_input_event(
        step_context,
        StepInputData(
            input_name=input_name,
            type_check_data=TypeCheckData(
                success=success,
                label=input_name,
                description=type_check.description if type_check else None,
                metadata_entries=type_check.metadata_entries if type_check else [],
            ),
        ),
    )


def _type_checked_event_sequence_for_input(
    step_context: SystemStepExecutionContext, input_name: str, input_value: Any
) -> Iterator[DagsterEvent]:
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.str_param(input_name, "input_name")

    step_input = step_context.step.step_input_named(input_name)
    input_def = step_input.source.get_input_def(step_context.pipeline_def)
    dagster_type = input_def.dagster_type
    with user_code_error_boundary(
        DagsterTypeCheckError,
        lambda: (
            f'Error occurred while type-checking input "{input_name}" of solid '
            f'"{str(step_context.step.solid_handle)}", with Python type {type(input_value)} and '
            f"Dagster type {dagster_type.display_name}"
        ),
    ):
        type_check = _do_type_check(step_context.for_type(dagster_type), dagster_type, input_value)

    yield _create_step_input_event(
        step_context, input_name, type_check=type_check, success=type_check.success
    )

    if not type_check.success:
        raise DagsterTypeCheckDidNotPass(
            description=(
                f'Type check failed for step input "{input_name}" - '
                f'expected type "{dagster_type.display_name}". '
                f"Description: {type_check.description}."
            ),
            metadata_entries=type_check.metadata_entries,
            dagster_type=dagster_type,
        )


def _type_check_output(
    step_context: SystemStepExecutionContext,
    step_output_handle: StepOutputHandle,
    output: Any,
    version: Optional[str],
) -> Iterator[DagsterEvent]:
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.inst_param(output, "output", (Output, DynamicOutput))

    step_output = step_context.step.step_output_named(output.output_name)
    step_output_def = step_context.solid_def.output_def_named(step_output.name)

    dagster_type = step_output_def.dagster_type
    with user_code_error_boundary(
        DagsterTypeCheckError,
        lambda: (
            f'Error occurred while type-checking output "{output.output_name}" of solid '
            f'"{str(step_context.step.solid_handle)}", with Python type {type(output.value)} and '
            f"Dagster type {dagster_type.display_name}"
        ),
    ):
        type_check = _do_type_check(step_context.for_type(dagster_type), dagster_type, output.value)

    yield DagsterEvent.step_output_event(
        step_context=step_context,
        step_output_data=StepOutputData(
            step_output_handle=step_output_handle,
            type_check_data=TypeCheckData(
                success=type_check.success,
                label=step_output_handle.output_name,
                description=type_check.description if type_check else None,
                metadata_entries=type_check.metadata_entries if type_check else [],
            ),
            version=version,
            metadata_entries=[
                entry for entry in output.metadata_entries if isinstance(entry, EventMetadataEntry)
            ],
        ),
    )

    if not type_check.success:
        raise DagsterTypeCheckDidNotPass(
            description='Type check failed for step output "{output_name}" - expected type "{dagster_type}".'.format(
                output_name=output.output_name,
                dagster_type=dagster_type.display_name,
            ),
            metadata_entries=type_check.metadata_entries,
            dagster_type=dagster_type,
        )


def core_dagster_event_sequence_for_step(
    step_context: SystemStepExecutionContext, prior_attempt_count: int
) -> Iterator[DagsterEvent]:
    """
    Execute the step within the step_context argument given the in-memory
    events. This function yields a sequence of DagsterEvents, but without
    catching any exceptions that have bubbled up during the computation
    of the step.
    """
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.int_param(prior_attempt_count, "prior_attempt_count")
    if prior_attempt_count > 0:
        yield DagsterEvent.step_restarted_event(step_context, prior_attempt_count)
    else:
        yield DagsterEvent.step_start_event(step_context)

    inputs = {}
    input_assets = []

    for step_input in step_context.step.step_inputs:
        input_def = step_input.source.get_input_def(step_context.pipeline_def)
        dagster_type = input_def.dagster_type

        if dagster_type.kind == DagsterTypeKind.NOTHING:
            continue

        input_assets.extend(step_input.source.get_asset_keys_and_partitions(step_context))

        for event_or_input_value in ensure_gen(step_input.source.load_input_object(step_context)):
            if isinstance(event_or_input_value, DagsterEvent):
                yield event_or_input_value
            else:
                check.invariant(step_input.name not in inputs)
                inputs[step_input.name] = event_or_input_value

    for input_name, input_value in inputs.items():
        for evt in check.generator(
            _type_checked_event_sequence_for_input(step_context, input_name, input_value)
        ):
            yield evt

    input_assets = _dedup_asset_partitions(input_assets)
    with time_execution_scope() as timer_result:
        user_event_sequence = check.generator(
            _user_event_sequence_for_step_compute_fn(step_context, inputs)
        )

        # It is important for this loop to be indented within the
        # timer block above in order for time to be recorded accurately.
        for user_event in check.generator(
            _step_output_error_checked_user_event_sequence(step_context, user_event_sequence)
        ):

            if isinstance(user_event, (Output, DynamicOutput)):
                for evt in _type_check_and_store_output(step_context, user_event, input_assets):
                    yield evt
            # for now, I'm ignoring AssetMaterializations yielded manually, but we might want
            # to do something with these in the above path eventually
            elif isinstance(user_event, (AssetMaterialization, Materialization)):
                yield DagsterEvent.step_materialization(step_context, user_event, input_assets)
            elif isinstance(user_event, ExpectationResult):
                yield DagsterEvent.step_expectation_result(step_context, user_event)
            else:
                check.failed(
                    "Unexpected event {event}, should have been caught earlier".format(
                        event=user_event
                    )
                )

    yield DagsterEvent.step_success_event(
        step_context, StepSuccessData(duration_ms=timer_result.millis)
    )


def _type_check_and_store_output(
    step_context: SystemStepExecutionContext,
    output: Union[DynamicOutput, Output],
    input_assets: List[AssetPartitions],
) -> Iterator[DagsterEvent]:

    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.inst_param(output, "output", (Output, DynamicOutput))
    check.list_param(input_assets, "input_assets", AssetPartitions)

    mapping_key = output.mapping_key if isinstance(output, DynamicOutput) else None

    step_output_handle = StepOutputHandle(
        step_key=step_context.step.key, output_name=output.output_name, mapping_key=mapping_key
    )

    # If we are executing using the execute_in_process API, then we allow for the outputs of solids
    # to be directly captured to a dictionary after they are computed.
    if step_context.output_capture is not None:
        step_context.output_capture[step_output_handle] = output.value

    version = (
        step_context.execution_plan.resolve_step_output_versions().get(step_output_handle)
        if MEMOIZED_RUN_TAG in step_context.pipeline.get_definition().tags
        else None
    )

    for output_event in _type_check_output(step_context, step_output_handle, output, version):
        yield output_event

    for evt in _store_output(step_context, step_output_handle, output, input_assets):
        yield evt

    for evt in _create_type_materializations(step_context, output.output_name, output.value):
        yield evt


def _materializations_to_events(
    step_context: SystemStepExecutionContext,
    step_output_handle: StepOutputHandle,
    materializations: Iterator[AssetMaterialization],
    input_assets: List[AssetPartitions] = None,
) -> Iterator[DagsterEvent]:
    if materializations is not None:
        for materialization in ensure_gen(materializations):
            if not isinstance(materialization, AssetMaterialization):
                raise DagsterInvariantViolationError(
                    (
                        'IO manager on output "{output_name}" has returned '
                        'value "{value}" of type "{python_type}". The return type can only be '
                        "AssetMaterialization."
                    ).format(
                        output_name=step_output_handle.output_name,
                        value=repr(materialization),
                        python_type=type(materialization).__name__,
                    )
                )

            yield DagsterEvent.step_materialization(step_context, materialization, input_assets)


def _asset_partitions_for_output(
    output_context: OutputContext,
    output_def: OutputDefinition,
    output_manager: IOManager,
) -> Optional[AssetPartitions]:

    definition_asset = output_def.get_asset_key_and_partitions(output_context)
    manager_asset = output_manager.experimental_internal_get_output_asset_key_and_partitions(
        output_context
    )

    if definition_asset and manager_asset:
        raise DagsterInvariantViolationError(
            (
                'Both the OutputDefinition and the IOManager of output "{output_name}" on solid "{solid_name}" '
                "associate it with AssetPartitions. Either remove the asset_fn on the OutputDefinition "
                "or use an IOManager that does not specify AssetPartitions in its get_output_asset() "
                "function."
            ).format(output_name=output_def.name, solid_name=output_context.solid_def.name)
        )

    asset_partitions = definition_asset or manager_asset

    return asset_partitions


def _flatten_asset_partitions(asset_partitions: Optional[AssetPartitions]):
    if asset_partitions is None:
        return []
    if not asset_partitions.partitions:
        return [(asset_partitions.asset_key, None)]
    return [(asset_partitions.asset_key, p) for p in asset_partitions.partitions]


def _dedup_asset_partitions(asset_partitions: List[AssetPartitions]) -> List[AssetPartitions]:
    # TODO: we should also anticipate this logic getting more complicated if we add extra
    # information onto the AssetPartitions object, such as how it was generated
    key_partition_mapping: Dict[AssetKey, Set[str]] = defaultdict(set)

    for ap in asset_partitions:
        # TODO: what does it mean when an AssetKey is at one point associated with some partitions,
        # and at another point associated with none?
        if not ap.partitions:
            key_partition_mapping[ap.asset_key] |= set()
        for p in ap.partitions:
            key_partition_mapping[ap.asset_key].add(p)
    return [
        AssetPartitions(asset_key=k, partitions=list(ps)) for k, ps in key_partition_mapping.items()
    ]


def _store_output(
    step_context: SystemStepExecutionContext,
    step_output_handle: StepOutputHandle,
    output: Union[Output, DynamicOutput],
    input_assets: List[AssetPartitions],
) -> Iterator[DagsterEvent]:

    output_def = step_context.solid_def.output_def_named(step_output_handle.output_name)
    output_manager = step_context.get_io_manager(step_output_handle)
    output_context = step_context.get_output_context(step_output_handle)

    asset_partitions = _asset_partitions_for_output(output_context, output_def, output_manager)
    flat_assets = _flatten_asset_partitions(asset_partitions)
    metadata_mapping: Dict[str, List[str]] = {p: [] for key, p in flat_assets}

    with user_code_error_boundary(
        DagsterExecutionHandleOutputError,
        control_flow_exceptions=[Failure, RetryRequested],
        msg_fn=lambda: (
            f'Error occurred while handling output "{output_context.name}" of '
            f'step "{step_context.step.key}":'
        ),
        step_key=step_context.step.key,
        output_name=output_context.name,
    ):
        handle_output_res = output_manager.handle_output(output_context, output.value)

    manager_materializations = []
    manager_metadata_entries = []
    if handle_output_res is not None:
        for elt in ensure_gen(handle_output_res):
            if isinstance(elt, AssetMaterialization):
                manager_materializations.append(elt)
            elif isinstance(elt, (EventMetadataEntry, PartitionSpecificMetadataEntry)):
                manager_metadata_entries.append(elt)
            else:
                raise DagsterInvariantViolationError(
                    (
                        "IO manager on output {output_name} has returned "
                        "value {value} of type {python_type}. The return type can only be "
                        "one of AssetMaterialization, EventMetadataEntry, PartitionSpecificMetadataEntry."
                    ).format(
                        output_name=step_output_handle.output_name,
                        value=repr(elt),
                        python_type=type(elt).__name__,
                    )
                )

    for entry in output.metadata_entries + manager_metadata_entries:
        # if you target a given entry at a partition, only apply it to the requested partition
        # otherwise, apply it to all partitions
        if isinstance(entry, PartitionSpecificMetadataEntry):
            if entry.partition not in metadata_mapping:
                raise DagsterInvariantViolationError(
                    (
                        "Output {output_name} associated a metadata entry ({entry}) with the partition "
                        "`{partition}`, which is not one of the declared partition mappings ({partitions})."
                    ).format(
                        output_name=output_def.name,
                        entry=repr(entry),
                        partition=entry.partition,
                        partitions=list(metadata_mapping.keys()),
                    )
                )
            metadata_mapping[entry.partition].append(entry.entry)
        else:
            for partition in metadata_mapping.keys():
                metadata_mapping[partition].append(entry)

    for asset_key, partition in flat_assets:
        yield DagsterEvent.step_materialization(
            step_context,
            AssetMaterialization(
                asset_key=asset_key,
                partition=partition,
                metadata_entries=metadata_mapping[partition],
            ),
            input_assets,
        )

    # for now, do not factor this in to metadata stuff
    for materialization in manager_materializations:
        yield DagsterEvent.step_materialization(step_context, materialization, input_assets)

    yield DagsterEvent.handled_output(
        step_context,
        output_name=step_output_handle.output_name,
        manager_key=output_def.io_manager_key,
        message_override=f'Handled input "{step_output_handle.output_name}" using intermediate storage'
        if isinstance(output_manager, IntermediateStorageAdapter)
        else None,
        metadata_entries=[
            entry for entry in manager_metadata_entries if isinstance(entry, EventMetadataEntry)
        ],
    )


def _create_type_materializations(
    step_context: SystemStepExecutionContext, output_name: str, value: Any
) -> Iterator[DagsterEvent]:
    """If the output has any dagster type materializers, runs them."""

    step = step_context.step
    current_handle = step.solid_handle

    # check for output mappings at every point up the composition hierarchy
    while current_handle:
        solid_config = step_context.environment_config.solids.get(current_handle.to_string())
        current_handle = current_handle.parent

        if solid_config is None:
            continue

        for output_spec in solid_config.outputs.type_materializer_specs:
            check.invariant(len(output_spec) == 1)
            config_output_name, output_spec = list(output_spec.items())[0]
            if config_output_name == output_name:
                step_output = step.step_output_named(output_name)
                with user_code_error_boundary(
                    DagsterTypeMaterializationError,
                    msg_fn=lambda: (
                        "Error occurred during output materialization:"
                        f'\n    output name: "{output_name}"'
                        f'\n    solid invocation: "{step_context.solid.name}"'
                        f'\n    solid definition: "{step_context.solid_def.name}"'
                    ),
                ):
                    output_def = step_context.solid_def.output_def_named(step_output.name)
                    dagster_type = output_def.dagster_type
                    materializations = dagster_type.materializer.materialize_runtime_values(
                        step_context, output_spec, value
                    )

                for materialization in materializations:
                    if not isinstance(materialization, (AssetMaterialization, Materialization)):
                        raise DagsterInvariantViolationError(
                            (
                                "materialize_runtime_values on type {type_name} has returned "
                                "value {value} of type {python_type}. You must return an "
                                "AssetMaterialization."
                            ).format(
                                type_name=dagster_type.display_name,
                                value=repr(materialization),
                                python_type=type(materialization).__name__,
                            )
                        )

                    yield DagsterEvent.step_materialization(step_context, materialization)


def _user_event_sequence_for_step_compute_fn(
    step_context: SystemStepExecutionContext, evaluated_inputs: Dict[str, Any]
) -> Iterator[SolidOutputUnion]:
    check.inst_param(step_context, "step_context", SystemStepExecutionContext)
    check.dict_param(evaluated_inputs, "evaluated_inputs", key_type=str)

    gen = execute_core_compute(
        step_context.for_compute(),
        evaluated_inputs,
        step_context.solid_def.compute_fn,
    )

    for event in iterate_with_context(
        lambda: user_code_error_boundary(
            DagsterExecutionStepExecutionError,
            control_flow_exceptions=[Failure, RetryRequested],
            msg_fn=lambda: f'Error occurred while executing solid "{step_context.solid.name}":',
            step_key=step_context.step.key,
            solid_def_name=step_context.solid_def.name,
            solid_name=step_context.solid.name,
        ),
        gen,
    ):
        yield event
