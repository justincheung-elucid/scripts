# Copyright (c) Elucid Bioimaging
# ruff: noqa: E402, C901, PLR0912, PLR0915
# Copyright (c) Elucid Bioimaging

import argparse
import csv
import glob
import os
import shutil
import sys
from pathlib import Path

import pydicom
from django.db import transaction

sys.path.append(
    os.path.join(
        os.sep, os.path.abspath(__file__).split(os.sep)[1], os.path.abspath(__file__).split(os.sep)[2], 'EVServer'
    )
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from case_management_utils import force_console_logging

import settings

sys.path.append(settings.INSTANCE)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'EVServer.settings')
import django

django.setup()

from app.CAPgraph.models import (
    Folio,
    Location,
    Patient,
    PatientId,
    PatientNames,
    PatientReport,
    PatientReportInstance,
    PatientT,
    RejectedCases,
    Series,
    SeriesJobStatus,
    Sop,
    Study,
    StudyOrderType,
    Usage,
    WiImageSeries,
    WiSimpleMedications,
    WiSimpleObservations,
    WiSubjectsWithInfo,
    WiTargetDefinition,
    WiTargetDefinitionHistologySections,
    WiTargetDefinitionKeyImage,
    WiTargetDefinitionProbabilityMaps,
    WiTargetDefinitionRegions,
    WiTargetDefinitionValueMaps,
    WiUpdates,
    WiUpdatesProcessingParameters,
    WiUpdatesProcessingParametersStageSettings,
    WorkItem,
    WorkItemOrderType,
)


def delete_patient_data_from_file_system(args, case_mapping, local_instance_plan=None):
    if args.no_institution:
        delete_local_instance_paths(args, local_instance_plan)
    elif args.aws:
        delete_patient_data_from_aws(args, case_mapping)
    else:
        delete_patient_data_from_data_center(args, case_mapping)


def derived_dir(images_dir: Path) -> Path:
    """Mirror ev::fs::GetDerivedDir (evPaths.cpp): insert 'Derived' right before 'Images'."""
    parts = images_dir.parts
    idx = parts.index('Images')
    return Path(*parts[:idx], 'Derived', *parts[idx:])


def gather_local_instance_fs_targets(patient_id):
    """Compute on-disk paths for a patient with no institution (e.g. a local DICOM
    C-STORE test upload). delete_patient.py always deletes an entire patient (never a
    subset of its studies), so this is just the whole per-patient Images/Derived tree
    plus a Working Storage sweep -- no DB lookups needed, no per-series pruning needed.
    """
    images_dir = Path(settings.INSTANCE) / 'AppData' / 'Images' / patient_id
    working_storage_root = Path(settings.INSTANCE) / 'AppData' / 'Working Storage'
    wi_dirs = sorted(working_storage_root.glob(f'*/{patient_id}'))

    return {
        'images_dir': images_dir,
        'derived_dir': derived_dir(images_dir),
        'wi_dirs': wi_dirs,
    }


def delete_local_instance_paths(args, plan):
    if plan is None:
        return
    for d in (plan['images_dir'], plan['derived_dir'], *plan['wi_dirs']):
        if not d.exists():
            logger.info('Already absent: %s', d)
            continue
        if args.dry_run:
            logger.info('Would be deleting %s', d)
        else:
            logger.info('Deleting %s', d)
            shutil.rmtree(d)


def delete_patient_data(args, image_path, wi_path):
    if args.dry_run:
        logger.info('Would be deleting %s', image_path)
        if wi_path:
            logger.info('Would be deleting %s', wi_path)
    else:
        logger.info('Deleting %s', image_path)
        try:
            shutil.rmtree(image_path)
        except Exception as e:
            logger.error('Error deleting: %s', str(e))
            sys.exit(1)
        if wi_path:
            logger.info('Deleting %s', wi_path)
            try:
                shutil.rmtree(wi_path)
            except Exception as e:
                logger.error('Error deleting: %s', str(e))
                sys.exit(1)


def delete_patient_data_from_aws(args, case_mapping):
    logger.info('Deleting %s from file system at AWS', args.patient)

    analyst_map = {
        'BS': 'becky_spencer',
        'CW': 'Carolyn_Walker',
        'EH': 'emily_healy',
        'EJ': 'emily_jeffreys',
        'EO': 'Eileen_OConnor',
        'MA': 'melissa_auger',
        'MM': 'Mackenzie_Maxwell',
    }
    (institution, initials, _) = case_mapping[args.patient]

    # account for weirdness with institution
    if institution == 'MUSC':
        institution = 'MUSC_FFR'
    institution = institution.split('-')[0]

    image_path = os.path.join(settings.INSTANCE, 'AppData/Institutions', institution, 'Images', args.patient)
    if initials not in analyst_map:
        # have to search for the data
        base_wi_path = os.path.join(
            settings.INSTANCE,
            'AppData/Institutions',
            institution,
            'Working Storage',
        )
        patient_dirs = glob.glob(f'{base_wi_path}/*/{args.patient}')
        if patient_dirs:
            for patient_dir in patient_dirs:
                delete_patient_data(args, image_path, patient_dir)
        else:
            logger.info('Unable to locate %s on the file system', args.patient)
            delete_patient_data(args, image_path, '')
    else:
        wi_path = os.path.join(
            settings.INSTANCE,
            'AppData/Institutions',
            institution,
            'Working Storage',
            f'wi_{analyst_map[initials]}',
            args.patient,
        )
        delete_patient_data(args, image_path, wi_path)


def delete_patient_data_from_data_center(args, case_mapping):
    logger.info('Deleting %s from file system in data center', args.patient)
    (institution, _, group) = case_mapping[args.patient]
    institution = institution.split('-')[0]
    group_dir = f'group{int(group[5:])}'
    image_path = os.path.join(
        settings.INSTANCE, 'AppData/Institutions', institution, 'FFR3.0', group_dir, 'Images', args.patient
    )
    wi_path = os.path.join(
        settings.INSTANCE,
        'AppData/Institutions',
        institution,
        'FFR3.0',
        group_dir,
        'Working Storage',
        f'wilist_{group_dir}',
        args.patient,
    )
    delete_patient_data(args, image_path, wi_path)


@transaction.atomic
def delete_patient_data_from_db(args):
    patient_id = args.patient
    if args.dry_run:
        log_prefix = 'Would be'
    else:
        log_prefix = ''
    # Fetch patient and related workitems
    capgraph_patients = Patient.objects.filter(patient_id=patient_id)
    if not capgraph_patients:
        logger.info('No capgraph patient found with patient_id: %s', patient_id)
        capgraph_patients = []

    for capgraph_patient in capgraph_patients:
        capgraph_workitems = WorkItem.objects.filter(patient_id=capgraph_patient.id)

        # SeriesJobStatus.series is a PROTECT FK onto WiImageSeries, which blocks any
        # later WiImageSeries/WorkItem delete below if left in place -- clear it first.
        logger.info('%s deleting SeriesJobStatus for patient %d', log_prefix, capgraph_patient.id)
        if not args.dry_run:
            SeriesJobStatus.objects.filter(series__workitem__in=capgraph_workitems).delete()

        for workitem in capgraph_workitems:
            # Deleting related WI Updates and their related data
            wi_updates = WiUpdates.objects.filter(workitem_id=workitem.id)
            for update in wi_updates:
                stage_settings_to_delete = update.wiupdatesprocessingparameters_set.all()
                logger.info(
                    '%s deleting WiUpdatesProcessingParametersStageSettings for %s',
                    log_prefix,
                    stage_settings_to_delete,
                )
                if not args.dry_run:
                    WiUpdatesProcessingParametersStageSettings.objects.filter(
                        processing_parameter_id__in=update.wiupdatesprocessingparameters_set.all()
                    ).delete()
                logger.info('%s deleting WiUpdatesProcessingParameters for %d', log_prefix, update.id)
                if not args.dry_run:
                    WiUpdatesProcessingParameters.objects.filter(wi_update_id=update.id).delete()
            logger.info('%s deleting WiUpdates for workitem %d', log_prefix, workitem.id)
            if not args.dry_run:
                WiUpdates.objects.filter(workitem_id=workitem.id).delete()

            # Deleting workitme order type
            if not args.dry_run:
                WorkItemOrderType.objects.filter(workitem_id=workitem.id).delete()

            # Deleting related patient reports and instances
            patient_reports = PatientReport.objects.filter(workitem_id=workitem.id)
            for report in patient_reports:
                logger.info('%s deleting PatientReportInstance for report %d', log_prefix, report.id)
                if not args.dry_run:
                    PatientReportInstance.objects.filter(report_id=report.id).delete()
            logger.info('%s deleting PatientReport for workitem %d', log_prefix, workitem.id)
            if not args.dry_run:
                PatientReport.objects.filter(workitem_id=workitem.id).delete()

            # Deleting other related workitem data
            logger.info('%s deleting WiImageSeries for workitem %d', log_prefix, workitem.id)
            if not args.dry_run:
                WiImageSeries.objects.filter(workitem_id=workitem.id).delete()
            logger.info('%s deleting WiSimpleMedications for workitem %d', log_prefix, workitem.id)
            if not args.dry_run:
                WiSimpleMedications.objects.filter(workitem_id=workitem.id).delete()
            logger.info('%s deleting WiSimpleObservations for workitem %d', log_prefix, workitem.id)
            if not args.dry_run:
                WiSimpleObservations.objects.filter(workitem_id=workitem.id).delete()
            logger.info('%s deleting WiSubjectsWithInfo for workitem %d', log_prefix, workitem.id)
            if not args.dry_run:
                WiSubjectsWithInfo.objects.filter(workitem_id=workitem.id).delete()
            logger.info('%s deleting Usage for workitem %d', log_prefix, workitem.id)
            if not args.dry_run:
                Usage.objects.filter(wi_id=workitem.id).delete()

            target_definitions = WiTargetDefinition.objects.filter(workitem_id=workitem.id)
            for td in target_definitions:
                logger.info(
                    '%s log_prefix deleting HistologySections for workitem %d/target %d', log_prefix, workitem.id, td.id
                )
                if not args.dry_run:
                    WiTargetDefinitionHistologySections.objects.filter(target_definition_id=td.id).delete()
                logger.info(
                    '%s deleting WiTargetDefinitionKeyImage for workitem %d/target %d', log_prefix, workitem.id, td.id
                )
                if not args.dry_run:
                    WiTargetDefinitionKeyImage.objects.filter(target_definition_id=td.id).delete()
                logger.info('%s workitem %d/target %d', log_prefix, workitem.id, td.id)
                if not args.dry_run:
                    WiTargetDefinitionProbabilityMaps.objects.filter(target_definition_id=td.id).delete()
                logger.info(
                    '%s deleting WiTargetDefinitionRegions for workitem %d/target %d', log_prefix, workitem.id, td.id
                )
                if not args.dry_run:
                    WiTargetDefinitionRegions.objects.filter(target_definition_id=td.id).delete()
                logger.info(
                    '%s deleting WiTargetDefinitionValueMaps for workitem %d/target %d', log_prefix, workitem.id, td.id
                )
                if not args.dry_run:
                    WiTargetDefinitionValueMaps.objects.filter(target_definition_id=td.id).delete()
                logger.info('%s deleting WiTargetDefinition for workitem %d/target %d', log_prefix, workitem.id, td.id)
                if not args.dry_run:
                    WiTargetDefinition.objects.filter(workitem_id=workitem.id).delete()

            # Deleting the workitems
            logger.info('%s deleting WorkItem for patient %d', log_prefix, capgraph_patient.id)
            if not args.dry_run:
                WorkItem.objects.filter(patient_id=capgraph_patient.id).delete()

        # Deleting the patient
        logger.info('%s deleting capgraph patient for patient %d', log_prefix, capgraph_patient.id)
        if not args.dry_run:
            capgraph_patient.delete()

    if not args.delete_images:
        return

    # Delete from other related tables
    patient_ts = PatientT.objects.filter(medical_id=patient_id)
    if not patient_ts:
        logger.info('no DICOM patient found for %s', patient_id)
    for patient_t in patient_ts:
        logger.info('%s deleting DICOM PatientId for patient %s', log_prefix, patient_id)
        if not args.dry_run:
            PatientId.objects.filter(patient_fk=patient_t.patient_pk).delete()
        studies = Study.objects.filter(patient_fk=patient_t.patient_pk)
        for study in studies:
            logger.info('%s deleting rejected cases for patient %s', log_prefix, patient_id)
            if not args.dry_run:
                RejectedCases.objects.filter(study=study.study_pk).delete()
            logger.info('%s deleting study order types for patient %s', log_prefix, patient_id)
            if not args.dry_run:
                StudyOrderType.objects.filter(study=study.study_pk).delete()
            logger.info('%s deleting series for patient %s', log_prefix, patient_id)
            if not args.dry_run:
                Series.objects.filter(study_fk=study.study_pk).delete()
            logger.info('%s deleting Folio for patient %s', log_prefix, patient_id)
            if not args.dry_run:
                Folio.objects.filter(study_fk=study.study_pk).delete()
            logger.info('%s deleting Location for patient %s', log_prefix, patient_id)
            if not args.dry_run:
                Location.objects.filter(folio_fk__in=Folio.objects.filter(study_fk=study.study_pk)).delete()
            logger.info('%s deleting SPO for patient %s', log_prefix, patient_id)
            if not args.dry_run:
                Sop.objects.filter(
                    series_fk__in=Series.objects.filter(study_fk=study.study_pk), study_fk=study.study_pk
                ).delete()
        logger.info('%s deleting PatientNames for patient %s', log_prefix, patient_id)
        if not args.dry_run:
            PatientNames.objects.filter(patient_fk=patient_t.patient_pk).delete()
        logger.info('%s deleting studies for patient %s', log_prefix, patient_id)
        if not args.dry_run:
            studies.delete()
        logger.info('%s deleting DICOM Patient for patient %s', log_prefix, patient_id)
        if not args.dry_run:
            patient_t.delete()


def resolve_patient_from_source_dir(source_dir):
    for path in sorted(Path(source_dir).rglob('*.dcm')):
        ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        patient_id = getattr(ds, 'PatientID', None)
        if patient_id:
            logger.info('Auto-detected patient %s from %s', patient_id, path)
            return str(patient_id)
    logger.error('No .dcm files with a PatientID found under %s', source_dir)
    sys.exit(1)


def load_case_mapping_from_csv(csv_file):
    result_dict = {}
    try:
        with open(csv_file) as csvfile:
            csvreader = csv.DictReader(csvfile)
            for row in csvreader:
                try:
                    key = row['Case ID']
                    value = (row['Institution'], row['Initials'], row['Column1'])
                    result_dict[key] = value
                except KeyError as e:
                    logger.error('Error processing row: %s. Missing column: %s', row, str(e))
                    sys.exit(1)
        return result_dict
    except Exception as e:
        logger.error('Error reading file: %s', str(e))
        sys.exit(1)


logger = force_console_logging()
parser = argparse.ArgumentParser(description='Delete patient and all data linked to the patient')
parser.add_argument('-p', '--patient', dest='patient', default=None, help='patient name')
parser.add_argument(
    '--source_dir',
    dest='source_dir',
    default=None,
    help='directory of .dcm files to auto-detect --patient from (via PatientID), instead of passing it directly',
)
parser.add_argument(
    '--delete_from_file_system',
    dest='delete_from_file_system',
    action='store_true',
    help='delete data from file system',
)
parser.add_argument(
    '--aws',
    dest='aws',
    action='store_true',
    help='if delete_from_file_system is specified, this tells if we are deleting from AWS or the data center',
)
parser.add_argument(
    '--no_institution',
    dest='no_institution',
    action='store_true',
    help='if delete_from_file_system is specified, use this instead of --aws/--tracking_csv for a patient with '
    'no institution -- e.g. a local DICOM C-STORE test upload. Paths are derived from STUDY_T.UID_HASH/'
    'SERIES_T.UID_HASH (as EVStoreScp writes them) plus a Working Storage/*/<patient> sweep, no CSV needed.',
)
parser.add_argument(
    '--tracking_csv',
    dest='tracking_csv',
    default=None,
    help='csv that is used to locate the files on the file system. '
    'If --delete_from_file_system is given (and --no_institution is not) this must be specified',
)
parser.add_argument(
    '--delete_images',
    dest='delete_images',
    action='store_true',
    help='if specified also delete the DICOM images, default is to not delete them',
)
parser.add_argument(
    '--dry_run',
    dest='dry_run',
    action='store_true',
    help='if specified do not delete anything, just report on what would be deleted',
)
args = parser.parse_args()
if not args.patient and not args.source_dir:
    logger.error('Must specify one of --patient or --source_dir')
    sys.exit(1)
if args.patient and args.source_dir:
    logger.error('Specify only one of --patient or --source_dir, not both')
    sys.exit(1)
if not args.patient:
    args.patient = resolve_patient_from_source_dir(args.source_dir)

if args.delete_from_file_system and not args.no_institution and not args.tracking_csv:
    logger.error('if delete_from_file_system is specified without --no_institution, you MUST also specify tracking_csv')
    sys.exit(1)

case_mapping_dict = {}
if args.delete_from_file_system and not args.no_institution:
    case_mapping_dict = load_case_mapping_from_csv(args.tracking_csv)
    if args.patient not in case_mapping_dict:
        logger.error('No case mapping found for patient %s', args.patient)
        sys.exit(1)

local_instance_plan = None
if args.delete_from_file_system and args.no_institution:
    local_instance_plan = gather_local_instance_fs_targets(args.patient)

delete_patient_data_from_db(args)
if args.delete_from_file_system:
    delete_patient_data_from_file_system(args, case_mapping_dict, local_instance_plan)
