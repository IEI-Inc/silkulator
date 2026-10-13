/**
  ******************************************************************************
  * @file    stm32f4xx_qspi.h
  * @brief   QUADSPI firmware library header, StdPeriph-style driver.
  *
  * NOTE: This file is a RECONSTRUCTION, not a recovered original.
  *
  * Like DFSDM (see stm32f4xx_dfsdm.h for the longer explanation), a
  * StdPeriph-style "stm32f4xx_qspi.c/.h" matching the exact API this
  * tree's src/fw/drivers/stm32f412/qspi.c calls (QSPI_Init(),
  * QSPI_SetAddress(), etc., rather than HAL_QSPI_*) could not be found
  * publicly -- only the newer HAL-style stm32f4xx_hal_qspi.c is
  * available. This header/implementation (see stm32f4xx_qspi.c) was
  * written from scratch to provide exactly the API surface
  * src/fw/drivers/stm32f412/qspi.c and drivers/qspi_definitions.h call,
  * implemented directly against the real QUADSPI register map (CR, DCR,
  * SR, FCR, DLR, CCR, AR, ABR, DR, PSMKR, PSMAR, PIR, LPTR) and its real,
  * fully-named bit definitions (QUADSPI_CR_*, QUADSPI_DCR_*,
  * QUADSPI_CCR_*, QUADSPI_SR_*) as provided by the official
  * STMicroelectronics/cmsis_device_f4 stm32f412rx.h device header. Unlike
  * DFSDM, QUADSPI's register layout/bit semantics are standard across the
  * whole F4/F7 QUADSPI-equipped family and well documented, so this
  * reconstruction should be close to functionally exact; it just wraps
  * those bits in the StdPeriph-style call shape this tree expects.
  ******************************************************************************
  */

#pragma once

#include "stm32f4xx.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  uint32_t QSPI_SShift;    /*!< One of QSPI_SShift_* */
  uint32_t QSPI_Prescaler; /*!< Clock prescaler minus 1, 0-255 */
  uint32_t QSPI_CKMode;    /*!< One of QSPI_CKMode_* */
  uint32_t QSPI_CSHTime;   /*!< One of QSPI_CSHTime_* */
  uint32_t QSPI_FSize;     /*!< log2(flash size in bytes) - 1 */
  uint32_t QSPI_FSelect;   /*!< One of QSPI_FSelect_* */
  uint32_t QSPI_DFlash;    /*!< One of QSPI_DFlash_* */
} QSPI_InitTypeDef;

#define QSPI_SShift_NoShift        ((uint32_t)0x00000000)
#define QSPI_SShift_HalfCycleShift ((uint32_t)QUADSPI_CR_SSHIFT)

#define QSPI_CKMode_Mode0  ((uint32_t)0x00000000)
#define QSPI_CKMode_Mode3  ((uint32_t)QUADSPI_DCR_CKMODE)

#define QSPI_CSHTime_1Cycle  ((uint32_t)0x00000000)
#define QSPI_CSHTime_2Cycle  ((uint32_t)QUADSPI_DCR_CSHT_0)
#define QSPI_CSHTime_3Cycle  ((uint32_t)QUADSPI_DCR_CSHT_1)
#define QSPI_CSHTime_4Cycle  ((uint32_t)(QUADSPI_DCR_CSHT_0 | QUADSPI_DCR_CSHT_1))

#define QSPI_FSelect_1  ((uint32_t)0x00000000)
#define QSPI_FSelect_2  ((uint32_t)QUADSPI_CR_FSEL)

#define QSPI_DFlash_Disable  ((uint32_t)0x00000000)
#define QSPI_DFlash_Enable   ((uint32_t)QUADSPI_CR_DFM)

/* ---- Communication configuration (CCR) field values ---------------------- */

#define QSPI_ComConfig_IMode_NoInstruction ((uint32_t)0x00000000)
#define QSPI_ComConfig_IMode_1Line         ((uint32_t)QUADSPI_CCR_IMODE_0)
#define QSPI_ComConfig_IMode_2Line         ((uint32_t)QUADSPI_CCR_IMODE_1)
#define QSPI_ComConfig_IMode_4Line         ((uint32_t)QUADSPI_CCR_IMODE)

#define QSPI_ComConfig_ADMode_NoAddress ((uint32_t)0x00000000)
#define QSPI_ComConfig_ADMode_1Line     ((uint32_t)QUADSPI_CCR_ADMODE_0)
#define QSPI_ComConfig_ADMode_2Line     ((uint32_t)QUADSPI_CCR_ADMODE_1)
#define QSPI_ComConfig_ADMode_4Line     ((uint32_t)QUADSPI_CCR_ADMODE)

#define QSPI_ComConfig_ADSize_8bit   ((uint32_t)0x00000000)
#define QSPI_ComConfig_ADSize_16bit  ((uint32_t)QUADSPI_CCR_ADSIZE_0)
#define QSPI_ComConfig_ADSize_24bit  ((uint32_t)QUADSPI_CCR_ADSIZE_1)
#define QSPI_ComConfig_ADSize_32bit  ((uint32_t)QUADSPI_CCR_ADSIZE)

#define QSPI_ComConfig_ABMode_NoAlternateByte ((uint32_t)0x00000000)
#define QSPI_ComConfig_ABMode_4Line           ((uint32_t)QUADSPI_CCR_ABMODE)

#define QSPI_ComConfig_DMode_NoData ((uint32_t)0x00000000)
#define QSPI_ComConfig_DMode_1Line  ((uint32_t)QUADSPI_CCR_DMODE_0)
#define QSPI_ComConfig_DMode_2Line  ((uint32_t)QUADSPI_CCR_DMODE_1)
#define QSPI_ComConfig_DMode_4Line  ((uint32_t)QUADSPI_CCR_DMODE)

#define QSPI_ComConfig_FMode_Indirect_Write ((uint32_t)0x00000000)
#define QSPI_ComConfig_FMode_Indirect_Read  ((uint32_t)QUADSPI_CCR_FMODE_0)
#define QSPI_ComConfig_FMode_Auto_Polling   ((uint32_t)QUADSPI_CCR_FMODE_1)
#define QSPI_ComConfig_FMode_Memory_Mapped  ((uint32_t)QUADSPI_CCR_FMODE)

#define QSPI_ComConfig_DDRMode_Disable ((uint32_t)0x00000000)
#define QSPI_ComConfig_DDRMode_Enable  ((uint32_t)QUADSPI_CCR_DDRM)

#define QSPI_ComConfig_DHHC_Disable ((uint32_t)0x00000000)
#define QSPI_ComConfig_DHHC_Enable  ((uint32_t)QUADSPI_CCR_DHHC)

/* ---- Status flags --------------------------------------------------------- */

#define QSPI_FLAG_TC    ((uint32_t)QUADSPI_SR_TCF)
#define QSPI_FLAG_TE    ((uint32_t)QUADSPI_SR_TEF)
#define QSPI_FLAG_FT    ((uint32_t)QUADSPI_SR_FTF)
#define QSPI_FLAG_SM    ((uint32_t)QUADSPI_SR_SMF)
#define QSPI_FLAG_TO    ((uint32_t)QUADSPI_SR_TOF)
#define QSPI_FLAG_BUSY  ((uint32_t)QUADSPI_SR_BUSY)

/* ---- Auto-polling match mode ---------------------------------------------- */

#define QSPI_PMM_AND  ((uint32_t)QUADSPI_CR_PMM)
#define QSPI_PMM_OR   ((uint32_t)0x00000000)

/* ---- Function prototypes --------------------------------------------------- */

void QSPI_StructInit(QSPI_InitTypeDef *QSPI_InitStruct);
void QSPI_Init(QSPI_InitTypeDef *QSPI_InitStruct);
void QSPI_Cmd(FunctionalState NewState);

void QSPI_SetDataLength(uint32_t length);
void QSPI_SetAddress(uint32_t address);

void QSPI_SendData8(uint8_t data);
uint8_t QSPI_ReceiveData8(void);

FlagStatus QSPI_GetFlagStatus(uint32_t QSPI_FLAG);
void QSPI_ClearFlag(uint32_t QSPI_FLAG);

void QSPI_DMACmd(FunctionalState NewState);
void QSPI_SetFIFOThreshold(uint32_t threshold_bytes);

void QSPI_AutoPollingMode_SetInterval(uint16_t interval);
void QSPI_AutoPollingMode_Config(uint32_t match, uint32_t mask, uint32_t match_mode);
void QSPI_AutoPollingModeStopCmd(FunctionalState NewState);

void QSPI_MemoryMappedMode_SetTimeout(uint16_t timeout_cycles);

void QSPI_AbortRequest(void);

#ifdef __cplusplus
}
#endif
