/**
  ******************************************************************************
  * @file    stm32f4xx_dfsdm.h
  * @brief   DFSDM (Digital Filter for Sigma-Delta Modulators) firmware
  *          library header, STM32F412/413/423 StdPeriph-style driver.
  *
  * NOTE: This file is a RECONSTRUCTION, not a recovered original.
  *
  * The DFSDM peripheral was added to the STM32F4 line late (F410/412/413/
  *423 sub-family only), after ST had largely moved new parts to the HAL
  * driver model. A StdPeriph-style ("stm32f4xx_dfsdm.c/.h", matching the
  * API this tree's src/fw/drivers/mic/stm32/dfsdm.c actually calls --
  * DFSDM_FilterInit(), DFSDM_TransceiverInit(), etc. rather than
  * HAL_DFSDM_*) was genuinely never published by ST as far as could be
  * found (only the newer HAL-style stm32f4xx_hal_dfsdm.c is publicly
  * available), and it isn't present anywhere in this tree's recovered
  * vendor sources either.
  *
  * This header/implementation (see stm32f4xx_dfsdm.c) was written from
  * scratch to provide exactly the API surface src/fw/drivers/mic/stm32/
  * dfsdm.c and dfsdm_definitions.h call, implemented directly against the
  * real DFSDM register map (FLTCR1/CR2/ISR/ICR/JCHGR/FCR/JDATAR/RDATAR/
  * AWHTR/AWLTR/AWSR/AWCFR/EXMAX/EXMIN/CNVTIMR and CHCFGR1/CHCFGR2/
  * CHAWSCDR/CHWDATAR/CHDATINR) as laid out in the official
  * STMicroelectronics/cmsis_device_f4 stm32f412rx.h device header
  * (DFSDM_Filter_TypeDef / DFSDM_Channel_TypeDef, DFSDM1_Channel0-3,
  * DFSDM1_Filter0-1). Field/bit-position choices follow the documented
  * STM32F412 DFSDM peripheral behavior to the best of what's reproducible
  * without the actual silicon or reference manual text in hand, and are
  * clearly isolated here so they're easy to audit or correct.
  *
  * This microphone path is not exercised during boot, so getting the
  * firmware linking/running under QEMU does not depend on these bit
  * positions being perfect -- only on the API existing with sane,
  * self-consistent behavior.
  ******************************************************************************
  */

#pragma once

#include "stm32f4xx.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The real register struct (DFSDM_Filter_TypeDef) is already provided by
 * the CMSIS device header with full reference-manual field names
 * (FLTCR1, FLTCR2, ...). This tree's driver code (dfsdm.c/
 * dfsdm_definitions.h) expects a StdPeriph-style struct with short field
 * names instead (CR1, RDATAR, ...) -- same register layout/offsets,
 * different, shorter names, matching the convention StdPeriph used for
 * every other peripheral (e.g. ADC_TypeDef::CR1, not ADC_TypeDef::ADCCR1).
 * Because the layout is identical we just reinterpret the same base
 * addresses (DFSDM1_FilterN_BASE) as this struct instead of
 * DFSDM_Filter_TypeDef. */
typedef struct {
  __IO uint32_t CR1;      /*!< DFSDM control register 1,                    Address offset: 0x00 */
  __IO uint32_t CR2;      /*!< DFSDM control register 2,                    Address offset: 0x04 */
  __IO uint32_t ISR;      /*!< DFSDM interrupt and status register,         Address offset: 0x08 */
  __IO uint32_t ICR;      /*!< DFSDM interrupt flag clear register,         Address offset: 0x0C */
  __IO uint32_t JCHGR;     /*!< DFSDM injected channel group selection reg, Address offset: 0x10 */
  __IO uint32_t FCR;      /*!< DFSDM filter control register,               Address offset: 0x14 */
  __IO uint32_t JDATAR;    /*!< DFSDM data register for injected group,     Address offset: 0x18 */
  __IO uint32_t RDATAR;    /*!< DFSDM data register for regular group,      Address offset: 0x1C */
  __IO uint32_t AWHTR;     /*!< DFSDM analog watchdog high threshold reg,   Address offset: 0x20 */
  __IO uint32_t AWLTR;     /*!< DFSDM analog watchdog low threshold reg,    Address offset: 0x24 */
  __IO uint32_t AWSR;      /*!< DFSDM analog watchdog status register,      Address offset: 0x28 */
  __IO uint32_t AWCFR;     /*!< DFSDM analog watchdog clear flag register,  Address offset: 0x2C */
  __IO uint32_t EXMAX;     /*!< DFSDM extreme detector maximum register,    Address offset: 0x30 */
  __IO uint32_t EXMIN;     /*!< DFSDM extreme detector minimum register,    Address offset: 0x34 */
  __IO uint32_t CNVTIMR;   /*!< DFSDM conversion timer register,            Address offset: 0x38 */
} DFSDM_TypeDef;

#define DFSDM_Filter0        ((DFSDM_TypeDef *) DFSDM1_Filter0_BASE)
#define DFSDM_Filter1        ((DFSDM_TypeDef *) DFSDM1_Filter1_BASE)

#define DFSDM_Channel0       DFSDM1_Channel0
#define DFSDM_Channel1       DFSDM1_Channel1
#define DFSDM_Channel2       DFSDM1_Channel2
#define DFSDM_Channel3       DFSDM1_Channel3

/* ---- Transceiver (channel) init struct / enums -------------------------- */

typedef struct {
  uint32_t DFSDM_Interface;            /*!< Serial interface type, one of DFSDM_Interface_* */
  uint32_t DFSDM_Clock;                /*!< Clock source, one of DFSDM_Clock_* */
  uint32_t DFSDM_Input;                /*!< Channel input source, one of DFSDM_Input_* */
  uint32_t DFSDM_Redirection;          /*!< Internal input redirection, one of DFSDM_Redirection_* */
  uint32_t DFSDM_PackingMode;          /*!< Data packing mode, one of DFSDM_PackingMode_* */
  uint32_t DFSDM_DataRightShift;       /*!< Right-shift applied to channel input data (0-31) */
  int32_t  DFSDM_Offset;               /*!< 24-bit signed offset applied to channel input data */
  uint32_t DFSDM_CLKAbsenceDetector;   /*!< One of DFSDM_CLKAbsenceDetector_* */
  uint32_t DFSDM_ShortCircuitDetector; /*!< One of DFSDM_ShortCircuitDetector_* */
} DFSDM_TransceiverInitTypeDef;

#define DFSDM_Interface_SPI_RisingEdge     ((uint32_t)0x00000000)
#define DFSDM_Interface_SPI_FallingEdge    ((uint32_t)0x00001000)
#define DFSDM_Interface_Manchester_Rising  ((uint32_t)0x00002000)
#define DFSDM_Interface_Manchester_Falling ((uint32_t)0x00003000)

#define DFSDM_Clock_Internal              ((uint32_t)0x00000000)
#define DFSDM_Clock_External              ((uint32_t)0x00000002)
#define DFSDM_Clock_InternalInverted       ((uint32_t)0x00004000)

#define DFSDM_Input_External              ((uint32_t)0x00000000)
#define DFSDM_Input_Internal_Register      ((uint32_t)0x00000008)
#define DFSDM_Input_ADC_Output             ((uint32_t)0x00000010)

#define DFSDM_Redirection_Disabled         ((uint32_t)0x00000000)
#define DFSDM_Redirection_Enabled          ((uint32_t)0x00000001)

#define DFSDM_PackingMode_Standard         ((uint32_t)0x00000000)
#define DFSDM_PackingMode_Interleaved      ((uint32_t)0x00000008)
#define DFSDM_PackingMode_Dual             ((uint32_t)0x00000010)

#define DFSDM_CLKAbsenceDetector_Disable   ((uint32_t)0x00000000)
#define DFSDM_CLKAbsenceDetector_Enable    ((uint32_t)0x00000040)

#define DFSDM_ShortCircuitDetector_Disable ((uint32_t)0x00000000)
#define DFSDM_ShortCircuitDetector_Enable  ((uint32_t)0x00000080)

/* ---- Filter init struct / enums ------------------------------------------ */

typedef struct {
  uint32_t DFSDM_SincOrder;                   /*!< One of DFSDM_SincOrder_* */
  uint32_t DFSDM_FilterOversamplingRatio;      /*!< FOSR, 1-1024 */
  uint32_t DFSDM_IntegratorOversamplingRatio;  /*!< IOSR, 1-256 */
} DFSDM_FilterInitTypeDef;

#define DFSDM_SincOrder_FastSinc  ((uint32_t)0x00000000)
#define DFSDM_SincOrder_Sinc1     ((uint32_t)0x20000000)
#define DFSDM_SincOrder_Sinc2     ((uint32_t)0x40000000)
#define DFSDM_SincOrder_Sinc3     ((uint32_t)0x60000000)
#define DFSDM_SincOrder_Sinc4     ((uint32_t)0x80000000)
#define DFSDM_SincOrder_Sinc5     ((uint32_t)0xA0000000)

#define DFSDM_ClkOutSource_SysClock  ((uint32_t)0x00000000)
#define DFSDM_ClkOutSource_AudioClock ((uint32_t)0x00000002)

#define DFSDM_DMAConversionMode_Regular   ((uint32_t)0)
#define DFSDM_DMAConversionMode_Injected  ((uint32_t)1)

/* ---- Short-circuit detector clear-flag / status helpers ------------------ */

#define DFSDM_CLEARF_SCD_Channel0  ((uint32_t)0x00000001)
#define DFSDM_CLEARF_SCD_Channel1  ((uint32_t)0x00000002)
#define DFSDM_CLEARF_SCD_Channel2  ((uint32_t)0x00000004)
#define DFSDM_CLEARF_SCD_Channel3  ((uint32_t)0x00000008)

#define DFSDM_IT_SCD_Channel0  ((uint32_t)0x00000001)
#define DFSDM_IT_SCD_Channel1  ((uint32_t)0x00000002)
#define DFSDM_IT_SCD_Channel2  ((uint32_t)0x00000004)
#define DFSDM_IT_SCD_Channel3  ((uint32_t)0x00000008)

/* Regular-channel selector (the RCH[2:0] field value written to CR1) is
 * just the plain channel index. Extremes-detector-channel selector
 * (EXCH[7:0] field, in CR2) is a one-hot bitmask, one bit per channel. */
#define DFSDM_RegularChannel0  ((uint32_t)0)
#define DFSDM_RegularChannel1  ((uint32_t)1)
#define DFSDM_RegularChannel2  ((uint32_t)2)
#define DFSDM_RegularChannel3  ((uint32_t)3)

#define DFSDM_ExtremChannel0   ((uint32_t)0x01)
#define DFSDM_ExtremChannel1   ((uint32_t)0x02)
#define DFSDM_ExtremChannel2   ((uint32_t)0x04)
#define DFSDM_ExtremChannel3   ((uint32_t)0x08)

/* ---- Function prototypes -------------------------------------------------- */

void DFSDM_Cmd(FunctionalState NewState);
void DFSDM_ChannelCmd(DFSDM_Channel_TypeDef *DFSDM_Channel, FunctionalState NewState);
void DFSDM_FilterCmd(DFSDM_TypeDef *DFSDMx, FunctionalState NewState);

void DFSDM_TransceiverStructInit(DFSDM_TransceiverInitTypeDef *DFSDM_InitStruct);
void DFSDM_TransceiverInit(DFSDM_Channel_TypeDef *DFSDM_Channel,
                           DFSDM_TransceiverInitTypeDef *DFSDM_InitStruct);

void DFSDM_FilterStructInit(DFSDM_FilterInitTypeDef *DFSDM_FilterInitStruct);
void DFSDM_FilterInit(DFSDM_TypeDef *DFSDMx, DFSDM_FilterInitTypeDef *DFSDM_FilterInitStruct);

void DFSDM_ConfigClkOutputSource(uint32_t DFSDM_ClkOutSource);
void DFSDM_ConfigClkOutputDivider(uint32_t divider);

void DFSDM_SelectRegularChannel(DFSDM_TypeDef *DFSDMx, uint32_t DFSDM_Channel);
void DFSDM_FastModeCmd(DFSDM_TypeDef *DFSDMx, FunctionalState NewState);

void DFSDM_DMATransferConfig(DFSDM_TypeDef *DFSDMx, uint32_t DFSDM_DMAConversionMode,
                             FunctionalState NewState);
void DFSDM_RegularContinuousModeCmd(DFSDM_TypeDef *DFSDMx, FunctionalState NewState);
void DFSDM_StartSoftwareRegularConversion(DFSDM_TypeDef *DFSDMx);

void DFSDM_ConfigShortCircuitThreshold(DFSDM_Channel_TypeDef *DFSDM_Channel, uint8_t threshold);
void DFSDM_ClearShortCircuitFlag(uint32_t DFSDM_ClearFlag);
FlagStatus DFSDM_GetShortCircuitFlagStatus(uint32_t DFSDM_Flag);

#ifdef __cplusplus
}
#endif
