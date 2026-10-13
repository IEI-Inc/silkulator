/**
  ******************************************************************************
  * @file    stm32f4xx_dfsdm.c
  * @brief   DFSDM firmware library implementation.
  *
  * See the header's top-of-file comment: this is a from-scratch
  * reconstruction of the StdPeriph-style DFSDM driver API that this
  * tree's src/fw/drivers/mic/stm32/dfsdm.c calls, built directly against
  * the real DFSDM register map from the official
  * STMicroelectronics/cmsis_device_f4 device header. It is not a
  * recovered original ST source file.
  ******************************************************************************
  */

#include "stm32f4xx_dfsdm.h"

/* IMPORTANT CORRECTION: DFSDM_Channel_TypeDef (CHCFGR1/CHCFGR2/CHAWSCDR/...)
 * is the REAL, official CMSIS struct for this chip (reused as-is from
 * stm32f412rx.h, not reconstructed by us), so its bit-field macros
 * (DFSDM_CHCFGR1_*, DFSDM_CHAWSCDR_*) already exist there at the real,
 * documented bit positions and must not be redefined here.
 *
 * This file originally guessed several of those positions wrong --
 * DFSDMEN at bit0 (real: bit31), CKOUTSRC at bit1 (real: bit30), CHEN at
 * bit31 (real: bit7), SPICKSEL as a single bit14 flag (real: a 2-bit
 * field at bits2-3), DATPACK/DATMPX/SITP masks all at the wrong
 * positions -- and placed CKABEN/SCDEN in the wrong register entirely
 * (they're real CHCFGR1 bits, not CHAWSCDR bits; CHAWSCDR actually holds
 * AWFORD/AWFOSR/BKSCD/SCDT). All of that is fixed below by using the
 * real, CMSIS-provided names directly instead of local redefinitions. */

#define DFSDM_CHCFGR2_DTRBS_SHIFT  (3)
#define DFSDM_CHCFGR2_OFFSET_SHIFT (8)

/* CHAWSCDR_SCDT_MASK: real register field is named SCDT (short-circuit
 * detector threshold), same bits [7:0] we assumed -- kept as our own
 * convenience mask name since CMSIS exposes it as DFSDM_CHAWSCDR_SCDT. */
#define DFSDM_CHAWSCDR_SCDT_MASK   ((uint32_t)0x000000FF)

#define DFSDM_CR1_RSWSTART   ((uint32_t)(1u << 17))
#define DFSDM_CR1_RCONT      ((uint32_t)(1u << 19))
#define DFSDM_CR1_RDMAEN     ((uint32_t)(1u << 21))
#define DFSDM_CR1_RCH_SHIFT  (24)
#define DFSDM_CR1_RCH_MASK   ((uint32_t)(0x7u << 24))
#define DFSDM_CR1_FAST       ((uint32_t)(1u << 29))
#define DFSDM_CR1_JDMAEN     ((uint32_t)(1u << 5))

#define DFSDM_FCR_FORD_SHIFT (29)
#define DFSDM_FCR_FORD_MASK  ((uint32_t)0xE0000000)
#define DFSDM_FCR_FOSR_SHIFT (16)
#define DFSDM_FCR_FOSR_MASK  ((uint32_t)0x03FF0000)
#define DFSDM_FCR_IOSR_MASK  ((uint32_t)0x000000FF)

#define DFSDM_ISR_SCDF_SHIFT (20)

void DFSDM_Cmd(FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  if (NewState != DISABLE) {
    DFSDM_Channel0->CHCFGR1 |= DFSDM_CHCFGR1_DFSDMEN;
  } else {
    DFSDM_Channel0->CHCFGR1 &= ~DFSDM_CHCFGR1_DFSDMEN;
  }
}

void DFSDM_ChannelCmd(DFSDM_Channel_TypeDef *DFSDM_Channel, FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  if (NewState != DISABLE) {
    DFSDM_Channel->CHCFGR1 |= DFSDM_CHCFGR1_CHEN;
  } else {
    DFSDM_Channel->CHCFGR1 &= ~DFSDM_CHCFGR1_CHEN;
  }
}

void DFSDM_FilterCmd(DFSDM_TypeDef *DFSDMx, FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  // There's no distinct filter enable bit separate from the channel/global
  // enables on this peripheral; filters start producing data once the
  // channel feeding them and the module are both enabled. This call is
  // kept as a no-op entry point purely so callers that mirror the real
  // StdPeriph call sequence (matching other peripherals' Cmd() pattern)
  // still have something to call.
  (void)DFSDMx;
}

void DFSDM_TransceiverStructInit(DFSDM_TransceiverInitTypeDef *DFSDM_InitStruct) {
  DFSDM_InitStruct->DFSDM_Interface = DFSDM_Interface_SPI_RisingEdge;
  DFSDM_InitStruct->DFSDM_Clock = DFSDM_Clock_Internal;
  DFSDM_InitStruct->DFSDM_Input = DFSDM_Input_External;
  DFSDM_InitStruct->DFSDM_Redirection = DFSDM_Redirection_Disabled;
  DFSDM_InitStruct->DFSDM_PackingMode = DFSDM_PackingMode_Standard;
  DFSDM_InitStruct->DFSDM_DataRightShift = 0;
  DFSDM_InitStruct->DFSDM_Offset = 0;
  DFSDM_InitStruct->DFSDM_CLKAbsenceDetector = DFSDM_CLKAbsenceDetector_Disable;
  DFSDM_InitStruct->DFSDM_ShortCircuitDetector = DFSDM_ShortCircuitDetector_Disable;
}

void DFSDM_TransceiverInit(DFSDM_Channel_TypeDef *DFSDM_Channel,
                           DFSDM_TransceiverInitTypeDef *DFSDM_InitStruct) {
  uint32_t cfgr1 = DFSDM_Channel->CHCFGR1;
  cfgr1 &= ~(DFSDM_CHCFGR1_DATPACK_Msk | DFSDM_CHCFGR1_DATMPX_Msk |
             DFSDM_CHCFGR1_SITP_Msk | DFSDM_CHCFGR1_SPICKSEL_Msk |
             DFSDM_CHCFGR1_CKOUTSRC);
  /* DFSDM_PackingMode/_Input/_Interface are this driver's own small enum
   * constants (see the header), not raw register-shifted values, so each
   * is shifted into its real field position here rather than assuming
   * the caller pre-shifted it. */
  cfgr1 |= ((DFSDM_InitStruct->DFSDM_PackingMode << DFSDM_CHCFGR1_DATPACK_Pos) &
            DFSDM_CHCFGR1_DATPACK_Msk);
  cfgr1 |= ((DFSDM_InitStruct->DFSDM_Input << DFSDM_CHCFGR1_DATMPX_Pos) &
            DFSDM_CHCFGR1_DATMPX_Msk);
  cfgr1 |= ((DFSDM_InitStruct->DFSDM_Interface << DFSDM_CHCFGR1_SITP_Pos) &
            DFSDM_CHCFGR1_SITP_Msk);
  if (DFSDM_InitStruct->DFSDM_Clock == DFSDM_Clock_InternalInverted) {
    cfgr1 |= DFSDM_CHCFGR1_SPICKSEL_0;
  } else if (DFSDM_InitStruct->DFSDM_Clock == DFSDM_Clock_External) {
    cfgr1 |= DFSDM_CHCFGR1_CKOUTSRC;
  }
  DFSDM_Channel->CHCFGR1 = cfgr1;

  uint32_t cfgr2 = DFSDM_Channel->CHCFGR2;
  cfgr2 &= ~((uint32_t)0x1F << DFSDM_CHCFGR2_DTRBS_SHIFT);
  cfgr2 |= ((DFSDM_InitStruct->DFSDM_DataRightShift & 0x1F) << DFSDM_CHCFGR2_DTRBS_SHIFT);
  cfgr2 &= 0xFF;  // clear offset field
  cfgr2 |= (((uint32_t)DFSDM_InitStruct->DFSDM_Offset & 0x00FFFFFF) <<
            DFSDM_CHCFGR2_OFFSET_SHIFT);
  DFSDM_Channel->CHCFGR2 = cfgr2;

  /* CKABEN/SCDEN are real CHCFGR1 bits (not CHAWSCDR, which only holds
   * AWFORD/AWFOSR/BKSCD/SCDT) -- corrected to write the right register. */
  uint32_t cfgr1b = DFSDM_Channel->CHCFGR1;
  if (DFSDM_InitStruct->DFSDM_CLKAbsenceDetector != DFSDM_CLKAbsenceDetector_Disable) {
    cfgr1b |= DFSDM_CHCFGR1_CKABEN;
  } else {
    cfgr1b &= ~DFSDM_CHCFGR1_CKABEN;
  }
  if (DFSDM_InitStruct->DFSDM_ShortCircuitDetector != DFSDM_ShortCircuitDetector_Disable) {
    cfgr1b |= DFSDM_CHCFGR1_SCDEN;
  } else {
    cfgr1b &= ~DFSDM_CHCFGR1_SCDEN;
  }
  DFSDM_Channel->CHCFGR1 = cfgr1b;

  if (DFSDM_InitStruct->DFSDM_Redirection != DFSDM_Redirection_Disabled) {
    DFSDM_Channel->CHDATINR = 0;
  }
}

void DFSDM_FilterStructInit(DFSDM_FilterInitTypeDef *DFSDM_FilterInitStruct) {
  DFSDM_FilterInitStruct->DFSDM_SincOrder = DFSDM_SincOrder_Sinc3;
  DFSDM_FilterInitStruct->DFSDM_FilterOversamplingRatio = 1;
  DFSDM_FilterInitStruct->DFSDM_IntegratorOversamplingRatio = 1;
}

void DFSDM_FilterInit(DFSDM_TypeDef *DFSDMx, DFSDM_FilterInitTypeDef *DFSDM_FilterInitStruct) {
  uint32_t fcr = DFSDMx->FCR;
  fcr &= ~(DFSDM_FCR_FORD_MASK | DFSDM_FCR_FOSR_MASK | DFSDM_FCR_IOSR_MASK);
  fcr |= (DFSDM_FilterInitStruct->DFSDM_SincOrder & DFSDM_FCR_FORD_MASK);
  fcr |= (((DFSDM_FilterInitStruct->DFSDM_FilterOversamplingRatio - 1) << DFSDM_FCR_FOSR_SHIFT) &
          DFSDM_FCR_FOSR_MASK);
  fcr |= ((DFSDM_FilterInitStruct->DFSDM_IntegratorOversamplingRatio - 1) & DFSDM_FCR_IOSR_MASK);
  DFSDMx->FCR = fcr;
}

void DFSDM_ConfigClkOutputSource(uint32_t DFSDM_ClkOutSource) {
  if (DFSDM_ClkOutSource != DFSDM_ClkOutSource_SysClock) {
    DFSDM_Channel0->CHCFGR1 |= DFSDM_CHCFGR1_CKOUTSRC;
  } else {
    DFSDM_Channel0->CHCFGR1 &= ~DFSDM_CHCFGR1_CKOUTSRC;
  }
}

void DFSDM_ConfigClkOutputDivider(uint32_t divider) {
  uint32_t cfgr1 = DFSDM_Channel0->CHCFGR1;
  cfgr1 &= ~DFSDM_CHCFGR1_CKOUTDIV_Msk;
  // CKOUTDIV is encoded as (divider - 1), per the real register's standard
  // "divide by N" convention used elsewhere in this peripheral family.
  uint32_t div_minus_1 = (divider > 0) ? (divider - 1) : 0;
  cfgr1 |= ((div_minus_1 << DFSDM_CHCFGR1_CKOUTDIV_Pos) & DFSDM_CHCFGR1_CKOUTDIV_Msk);
  DFSDM_Channel0->CHCFGR1 = cfgr1;
}

void DFSDM_SelectRegularChannel(DFSDM_TypeDef *DFSDMx, uint32_t DFSDM_Channel) {
  uint32_t cr1 = DFSDMx->CR1;
  cr1 &= ~DFSDM_CR1_RCH_MASK;
  cr1 |= ((DFSDM_Channel & 0x7) << DFSDM_CR1_RCH_SHIFT);
  DFSDMx->CR1 = cr1;
}

void DFSDM_FastModeCmd(DFSDM_TypeDef *DFSDMx, FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  if (NewState != DISABLE) {
    DFSDMx->CR1 |= DFSDM_CR1_FAST;
  } else {
    DFSDMx->CR1 &= ~DFSDM_CR1_FAST;
  }
}

void DFSDM_DMATransferConfig(DFSDM_TypeDef *DFSDMx, uint32_t DFSDM_DMAConversionMode,
                             FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  uint32_t bit = (DFSDM_DMAConversionMode == DFSDM_DMAConversionMode_Injected) ?
      DFSDM_CR1_JDMAEN : DFSDM_CR1_RDMAEN;
  if (NewState != DISABLE) {
    DFSDMx->CR1 |= bit;
  } else {
    DFSDMx->CR1 &= ~bit;
  }
}

void DFSDM_RegularContinuousModeCmd(DFSDM_TypeDef *DFSDMx, FunctionalState NewState) {
  assert_param(IS_FUNCTIONAL_STATE(NewState));
  if (NewState != DISABLE) {
    DFSDMx->CR1 |= DFSDM_CR1_RCONT;
  } else {
    DFSDMx->CR1 &= ~DFSDM_CR1_RCONT;
  }
}

void DFSDM_StartSoftwareRegularConversion(DFSDM_TypeDef *DFSDMx) {
  DFSDMx->CR1 |= DFSDM_CR1_RSWSTART;
}

void DFSDM_ConfigShortCircuitThreshold(DFSDM_Channel_TypeDef *DFSDM_Channel, uint8_t threshold) {
  uint32_t awscdr = DFSDM_Channel->CHAWSCDR;
  awscdr &= ~DFSDM_CHAWSCDR_SCDT_MASK;
  awscdr |= threshold;
  DFSDM_Channel->CHAWSCDR = awscdr;
}

void DFSDM_ClearShortCircuitFlag(uint32_t DFSDM_ClearFlag) {
  // Short-circuit-detect flags for all channels of a given module are
  // cleared via that module's own filter instance ICR; channels 0/1 feed
  // filter0, so route the clear there (matches how this driver's only
  // caller, mic_selftest(), always clears DFSDM_Channel2's flag via
  // DFSDM_Filter0).
  DFSDM_Filter0->ICR |= (DFSDM_ClearFlag << DFSDM_ISR_SCDF_SHIFT);
}

FlagStatus DFSDM_GetShortCircuitFlagStatus(uint32_t DFSDM_Flag) {
  uint32_t scdf = (DFSDM_Filter0->ISR >> DFSDM_ISR_SCDF_SHIFT) & 0xFF;
  return ((scdf & DFSDM_Flag) != 0) ? SET : RESET;
}
