#!/usr/bin/env python3

import argparse
import pydicom
import pandas as pd

# ===== CLI =========================================
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("filepath_in", help="Path to DICOM file")
    parser.add_argument(
        "--hide-missing",
        action="store_true",
        help="Hide tags that are completely missing (None) from the printed tables",
    )
    return parser.parse_args()

# ===== CORE IMPLEMENTATION =========================
def main(args: argparse.Namespace):
    ds = pydicom.dcmread(args.filepath_in, stop_before_pixels=True)
    print_dicom_generic_metadata(ds, hide_missing=args.hide_missing)
    print("==============================================")

    rows = [
        {
            "tag": tag_to_string(tag),
            "name": name,
            "value": find_tag_value(ds, tag),
        }
        for name, tag in CARDIAC_PHASE_RELATED_TAGS.items()
    ]
    if args.hide_missing:
        rows = [row for row in rows if row["value"] is not None]

    # Prepare for display
    df = pd.DataFrame(rows).set_index("tag")
    print(df.to_string())
    print("None suggests the tag is completely missing, regardless of deidentification.)")
    print("Blank suggests the tag was:\n\t1. got stripped out either by not being on whitelist\n\t2. explicitly blanked out by whitelist, or\n\t3. actually blank to begin with.")

# ===== DETAILED IMPLEMENTATION =====================
def print_dicom_generic_metadata(ds: pydicom.FileDataset, hide_missing: bool = False):
    rows = [
        {
            "tag": tag_to_string(tag),
            "name": name,
            "value": find_tag_value(ds, tag),
        }
        for name, tag in GENERIC_METADATA_TAGS.items()
    ]
    if hide_missing:
        rows = [row for row in rows if row["value"] is not None]
    df = pd.DataFrame(rows).set_index("tag")
    print(df.to_string())

def tag_to_string(tag: tuple[int, int]):
    return f"({tag[0]:04X},{tag[1]:04X})"

def find_tag_value(ds: pydicom.Dataset, tag: tuple[int, int]):
    """Search ds and any nested sequence items for tag, since some GE cardiac
    attributes (e.g. under (0049,1001)) live inside a sequence item rather than
    at the top level."""
    for elem in ds.iterall():
        if elem.tag == tag:
            return elem.value
    return None

GENERIC_METADATA_TAGS = {
    "StudyInstanceUID": (0x0020, 0x000D),
    "SeriesInstanceUID": (0x0020, 0x000E),
    "Manufacturer": (0x0008, 0x0070),
    "ManufacturerModelName": (0x0008, 0x1090),
    "DeviceSerialNumber": (0x0018, 0x1000),
    "SoftwareVersions": (0x0018, 0x1020),
}

# Here is every tag we seem aware of related to cardiac phase.
CARDIAC_PHASE_RELATED_TAGS = {
    "DCM_NominalPercentageOfCardiacPhase": (0x0020, 0x9241),
    "DCM_ScanOptions": (0x0018, 0x0022),
    "EV_PHILLIPS_PHASE_TAG": (0x01E1, 0x1020),
    # Canon private tag: cardiac phase as a percentage string (e.g. "80% ")
    "EV_CANON_PHASE_PERCENTAGE_TAG": (0x7005, 0x1004),
    # Canon private tag: cardiac phase time offset in ms relative to the R-R interval
    "EV_CANON_PHASE_TIME_MS_TAG": (0x7005, 0x1005),
    # Canon private tag: R-R interval duration in ms, used with EV_CANON_PHASE_TIME_MS_TAG
    "EV_CANON_RR_INTERVAL_TAG": (0x7005, 0x1003),
    # GE private tag (GEMS_CT_CARDIAC_001 block): PhaseLocation, part of the CT Cardiac
    # Sequence. Only present when the scanner's cardiac option is installed; private
    # creator (0049,0010) must equal "GEMS_CT_CARDIAC_001" for this to be reliable.
    "EV_GE_CARDIAC_PHASE_LOCATION_TAG": (0x0049, 0x1023),

    # Standard DICOM Cardiac Synchronization Module/Sequence attributes (PS3.3 C.7.6.18.1
    # for the legacy single-frame tags; the Enhanced CT/MR multi-frame Cardiac
    # Synchronization Sequence (0018,9118) for the rest).
    "DCM_TriggerTime": (0x0018, 0x1060),
    # Retired; from the NM Multi-gated Acquisition Module (PS3.3 C.8.4.13) — average
    # duration of accepted beats, in msec (the vendor-neutral "R-R interval" value).
    "DCM_NominalInterval": (0x0018, 0x1062),
    "DCM_FrameTime": (0x0018, 0x1063),
    # Cardiac Synchronization Module; defined term PCNT = "percentage of R-R forward
    # from trigger", i.e. how to interpret the percent-phase tag below.
    "DCM_CardiacFramingType": (0x0018, 0x1064),
    "DCM_LowRRValue": (0x0018, 0x1081),
    "DCM_HighRRValue": (0x0018, 0x1082),
    "DCM_IntervalsAcquired": (0x0018, 0x1083),
    "DCM_IntervalsRejected": (0x0018, 0x1084),
    "DCM_HeartRate": (0x0018, 0x1088),
    "DCM_CardiacSynchronizationTechnique": (0x0018, 0x9037),
    "DCM_CardiacRRIntervalSpecified": (0x0018, 0x9070),
    "DCM_CardiacSynchronizationSequence": (0x0018, 0x9118),
    # Frame Content Sequence; "description of the position in the cardiac cycle
    # that is most representative of this frame" — the most literal vendor-neutral
    # "which phase" descriptor.
    "DCM_CardiacCyclePosition": (0x0018, 0x9236),
    # Formally scoped to PET dynamic/fMRI use cases, but conceptually an ordinal
    # phase/frame index — borrowed-usage candidate like (0020,9241).
    "DCM_TemporalPositionIndex": (0x0020, 0x9128),
    "DCM_NominalCardiacTriggerDelayTime": (0x0020, 0x9153),
    "DCM_NominalCardiacTriggerTimePriorToRPeak": (0x0020, 0x9154),
    "DCM_ActualCardiacTriggerTimePriorToRPeak": (0x0020, 0x9155),
    "DCM_RRIntervalTimeNominal": (0x0020, 0x9251),
    "DCM_ActualCardiacTriggerDelayTime": (0x0020, 0x9252),

    # Standard DICOM NM (Nuclear Medicine) Multi-frame Module gating attributes
    # (PS3.3 C.8.4.8). Formally scoped to NM gated acquisitions; presence on a CT
    # image would be a borrowed/non-conformant usage, same pattern as (0020,9241).
    "DCM_PhaseVector": (0x0054, 0x0030),
    "DCM_NumberOfPhases": (0x0054, 0x0031),
    "DCM_RRIntervalVector": (0x0054, 0x0060),
    "DCM_NumberOfRRIntervals": (0x0054, 0x0061),
    # Time Slot = which phase-bin of the averaged heartbeat, as opposed to R-R
    # Interval above (which beat-duration bucket, e.g. normal vs. ectopic). Likely
    # the closer conceptual match for "which gate" / "total gates".
    "DCM_TimeSlotVector": (0x0054, 0x0070),
    "DCM_NumberOfTimeSlots": (0x0054, 0x0071),

    # GE private tags (GEMS_HELIOS_01 block, group 0045) — cardiac recon parameters.
    "EV_GE_CardiacReconAlgorithm": (0x0045, 0x1030),
    "EV_GE_AvgHeartRateForImage": (0x0045, 0x1031),
    "EV_GE_TemporalResolution": (0x0045, 0x1032),
    "EV_GE_PctRpeakDelay": (0x0045, 0x1033),
    "EV_GE_ActualPctRpeakDelay": (0x0045, 0x1034),
    "EV_GE_EkgFullMaStartPhase": (0x0045, 0x1036),
    "EV_GE_EkgFullMaEndPhase": (0x0045, 0x1037),
    "EV_GE_EkgModulationMaxMa": (0x0045, 0x1038),
    "EV_GE_EkgModulationMinMa": (0x0045, 0x1039),
    "EV_GE_NoiseReductionImageFilterDesc": (0x0045, 0x103B),
    "EV_GE_RPeakTimeDelay": (0x0045, 0x103F),
    "EV_GE_ActualRPeakTimeDelay": (0x0045, 0x1044),
    "EV_GE_CardiacScanOptions": (0x0045, 0x1045),

    # GE private tags (GEMS_CT_CARDIAC_001 block, group 0049) — CT Cardiac Sequence.
    "EV_GE_HeartRateAtConfirm": (0x0049, 0x1002),
    "EV_GE_AvgHeartRatePriorToConfirm": (0x0049, 0x1003),
    "EV_GE_MinHeartRatePriorToConfirm": (0x0049, 0x1004),
    "EV_GE_MaxHeartRatePriorToConfirm": (0x0049, 0x1005),
    "EV_GE_NumReconSectors": (0x0049, 0x100B),
    "EV_GE_RpeakTimeStamps": (0x0049, 0x100C),
    "EV_GE_EkgGatingType": (0x0049, 0x1016),
    "EV_GE_EkgWaveTimeOffFirstDataPoint": (0x0049, 0x101B),
}

# ===== BOILERPLATE =================================
if __name__ == "__main__":
    main(parse_args())

