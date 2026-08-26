/**
 * Committed code fixture for the source pane's Code tab.
 *
 * A verbatim slice of the indexed snapshot:
 *   repo   openAUTOSAR/classic-platform  (GPL-2.0, Arctic Core)
 *   sha    09433770bebb8f27a7b480d7c96d814c68ffed3e
 *   path   communication/CanIf/src/CanIf.c
 *   lines  715-885 of 1927
 *
 * It is committed rather than fetched because GET /code/{path} (spec §6) does
 * not exist yet and data/ is gitignored. Nothing else about the Code tab is
 * fixture-shaped: the highlighter, the gutter, the annotation bars and the
 * evidence band all read the same fields a real `citation` event carries, so
 * swapping this constant for a fetched slice is the only change WP4 needs.
 *
 * Generated, not hand-written: it is a quotation.
 */

export const CANIF_FIXTURE_PATH = "communication/CanIf/src/CanIf.c";
export const CANIF_FIXTURE_SHA = "09433770bebb8f27a7b480d7c96d814c68ffed3e";

/** 1-based line number of the first line of CANIF_FIXTURE_SOURCE. */
export const CANIF_FIXTURE_FIRST_LINE = 715;

/** Total lines in the real file, for the "n lines" readout in the crumb bar. */
export const CANIF_FIXTURE_TOTAL_LINES = 1927;

export const CANIF_FIXTURE_SOURCE = `        CanIf_ControllerModeType *ControllerModePtr) {

    /* !req 4.0.3/CANIF316 */

    /* @req 4.0.3/CANIF661 */
    VALIDATE_RV((TRUE == CanIf_Global.initRun), CANIF_GET_CONTROLLER_MODE_ID, CANIF_E_UNINIT, E_NOT_OK);
    /* @req 4.0.3/CANIF313 */
    VALIDATE_RV((ControllerId < CANIF_CHANNEL_CNT), CANIF_GET_CONTROLLER_MODE_ID, CANIF_E_PARAM_CONTROLLERID, E_NOT_OK);
    /* @req 4.0.3/CANIF656 */
    VALIDATE_RV((NULL != ControllerModePtr), CANIF_GET_CONTROLLER_MODE_ID, CANIF_E_PARAM_POINTER, E_NOT_OK);

    /* @req 4.0.3/CANIF541 */
    *ControllerModePtr = CanIf_Global.channelData[ControllerId].ControllerMode;

    return E_OK;
}

/**
 *
 * @param canTxPduId
 * @param pduInfoPtr
 * @return
 */
/* @req 4.0.3/CANIF005 */
Std_ReturnType CanIf_Transmit(PduIdType CanTxPduId,
        const PduInfoType *PduInfoPtr) {
    /* !req 4.0.3/CANIF323 */
    /* @req 4.0.3/CANIF075 */
    /* !req 4.0.3/CANIF666 */
    /* !req CANIF058 */

    Can_PduType canPdu;
    Can_ReturnType writeRet;
    Std_ReturnType ret;
    Std_ReturnType status;
    status = E_NOT_OK;
    ret = E_OK;

    /* @req 4.0.3/CANIF661 */
    VALIDATE_RV((TRUE == CanIf_Global.initRun), CANIF_TRANSMIT_ID, CANIF_E_UNINIT, E_NOT_OK);
    /* @req 4.0.3/CANIF320 */
    VALIDATE_RV((NULL != PduInfoPtr), CANIF_TRANSMIT_ID, CANIF_E_PARAM_POINTER, E_NOT_OK);
    /* @req 4.0.3/CANIF319 */
    VALIDATE_RV((CanTxPduId < CanIf_ConfigPtr->InitConfig->CanIfNumberOfCanTXPduIds), CANIF_TRANSMIT_ID, CANIF_E_INVALID_TXPDUID, E_NOT_OK);

    const CanIf_TxPduConfigType *txPduPtr = &CanIf_ConfigPtr->InitConfig->CanIfTxPduConfigPtr[CanTxPduId];
    uint8 controller = (uint8)txPduPtr->CanIfTxPduBufferRef->CanIfBufferHthRef->CanIfCanControllerIdRef;
/*############################################################################################*/
    /* IMPROVEMENT: Cleanup needed... */
    /* @req 4.0.3/CANIF382 */
    /* @req 4.0.3/CANIF073 Part of */
    /* @req 4.0.3/CANIF491 Part of */
    if( CANIF_GET_OFFLINE == CanIf_Global.channelData[controller].PduMode || CANIF_GET_RX_ONLINE == CanIf_Global.channelData[controller].PduMode ) {
        DET_REPORT_ERROR(CANIF_TRANSMIT_ID, CANIF_E_STOPPED);
        return E_NOT_OK;
    }
    /* @req 4.0.3/CANIF723 */
    /* @req 4.0.3/CANIF677 */ //Return
    if( CANIF_CS_STOPPED == CanIf_Global.channelData[controller].ControllerMode ) {
        DET_REPORT_ERROR(CANIF_TRANSMIT_ID, CANIF_E_STOPPED);
        return E_NOT_OK;
    }
    /* @req 4.0.3/CANIF317 */
    /* @req 4.0.3/CANIF491 Part of */
    /* @req 4.0.3/CANIF489 Part of*///Return
    if( (CANIF_CS_STARTED != CanIf_Global.channelData[controller].ControllerMode) ||
        (CANIF_GET_RX_ONLINE == CanIf_Global.channelData[controller].PduMode) ||
            (CANIF_GET_OFFLINE == CanIf_Global.channelData[controller].PduMode) ) {
        ret = E_NOT_OK;
    }
    if (ret == E_OK){
/*############################################################################################*/
#if (CANIF_PUBLIC_PN_SUPPORT == STD_ON)
        /* @req 4.0.3/CANIF750 */
        if ((CanIf_ConfigPtr->Arc_ChannelConfig[controller].CanIfCtrlPnFilterSet) && (CanIf_Global.channelData[controller].pnTxFilterEnabled)
                && (!txPduPtr->CanIfTxPduPnFilterEnable ))
        {
            ret = E_NOT_OK;
        }
#endif
        if (ret == E_OK){
            /* @req 4.0.3/CANIF072 */
            /* @req 4.0.3/CANIF491 Part of */
            /* !req 4.0.3/CANIF437 */
            if( (CANIF_GET_OFFLINE_ACTIVE == CanIf_Global.channelData[controller].PduMode) ||
                    (CANIF_GET_OFFLINE_ACTIVE_RX_ONLINE == CanIf_Global.channelData[controller].PduMode) ) {
                if((NO_FUNCTION_CALLOUT != txPduPtr->CanIfUserTxConfirmation) &&
                    (CanIfUserTxConfirmations[txPduPtr->CanIfUserTxConfirmation] != NULL)){
                    CanIfUserTxConfirmations[txPduPtr->CanIfUserTxConfirmation](txPduPtr->CanIfTxPduId);
                }
                status = E_OK;
            }
            if (status == E_NOT_OK) {
                /* @req 4.0.3/CANIF318 */
                canPdu.id = txPduPtr->CanIfCanTxPduIdCanId;
                /* @req 4.0.3/CANIF243 */
                if( CANIF_CAN_ID_TYPE_29 == txPduPtr->CanIfTxPduIdCanIdType ) {
                    canPdu.id |= (1ul << EXT_ID_BIT_POS);
                }
                else if( CANIF_CAN_FD_ID_TYPE_11 == txPduPtr->CanIfTxPduIdCanIdType ) {
                    canPdu.id |= (1ul << CAN_FD_BIT_POS); /*setting FD flag*/
                }
                else if( CANIF_CAN_FD_ID_TYPE_29 == txPduPtr->CanIfTxPduIdCanIdType ) {
                    canPdu.id |= (3ul << CAN_FD_BIT_POS); /*setting both IDE and FD flag*/
                }


                /* Dynamic DLC length */
                if (PduInfoPtr->SduLength < txPduPtr->CanIfCanTxPduIdDlc) {
                    canPdu.length = PduInfoPtr->SduLength;
                } else {
                    canPdu.length = txPduPtr->CanIfCanTxPduIdDlc;
                }
                canPdu.sdu = PduInfoPtr->SduDataPtr;
                canPdu.swPduHandle = CanTxPduId;

                writeRet = Can_Write(txPduPtr->CanIfTxPduBufferRef->CanIfBufferHthRef->CanIfHthIdSymRef, &canPdu);
                if( CAN_BUSY == writeRet ) {
                    ret = E_NOT_OK;

            #if (CANIF_PUBLIC_TX_BUFFERING == STD_ON)
                    /* @req CANIF381 */
                    /* @req CANIF835 */
                    /* @req CANIF063 */
                    /* Should not really have to check for BASIC here since if buffer size greater than 0
                    * and FULLCAN violates req CANIF834_Conf */
                    if( (0 != txPduPtr->CanIfTxPduBufferRef->CanIfBufferSize) &&
                        (CANIF_HANDLE_TYPE_BASIC == txPduPtr->CanIfTxPduBufferRef->CanIfBufferHthRef->CanIfHthType) ) {
                        ret = qReplaceOrAdd(txPduPtr->CanIfTxPduBufferRef, &canPdu, TRUE);
                    }
            #endif
                } else if( CAN_NOT_OK == writeRet ) {
                    /* Nothing to do. Throw message */
                    ret = E_NOT_OK;
                } else if( CAN_OK == writeRet ) {
                    /* @req 4.0.3/CANIF162 */
                    ret = E_OK;
                }
            }else {
                ret = status;
            }
        }
    }
    return ret;
}

/* @req 4.0.3/CANIF007 */
void CanIf_TxConfirmation(PduIdType canTxPduId) {
    const CanIf_TxPduConfigType *txPduPtr;

    /* !req 4.0.3/CANIF413 */
    /* !req 4.0.3/CANIF414 */
    /* !req CANIF058 */

    /* @req 4.0.3/CANIF412 */
    /* @req 4.0.3/CANIF661 */
    VALIDATE_NO_RV((TRUE == CanIf_Global.initRun), CANIF_TXCONFIRMATION_ID, CANIF_E_UNINIT);
    /* @req 4.0.3/CANIF410 */
    VALIDATE_NO_RV((canTxPduId < CanIf_ConfigPtr->InitConfig->CanIfNumberOfCanTXPduIds), CANIF_TXCONFIRMATION_ID, CANIF_E_PARAM_LPDU);

    txPduPtr = &CanIf_ConfigPtr->InitConfig->CanIfTxPduConfigPtr[canTxPduId];

#if (CANIF_PUBLIC_TXCONFIRM_POLLING_SUPPORT == STD_ON)
    /* !req 4.0.3/CANIF740 */
#endif

    CanIf_PduGetModeType mode;
    if( E_OK == CanIf_GetPduMode(txPduPtr->CanIfTxPduBufferRef->CanIfBufferHthRef->CanIfCanControllerIdRef, &mode) ) {
        /* @req 4.0.3/CANIF489*/
        /* @req 4.0.3/CANIF491 Part of */
        /* @req 4.0.3/CANIF075 */`;
