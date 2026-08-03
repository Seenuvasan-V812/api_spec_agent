package com.example.quotes;

import java.math.BigDecimal;
import java.time.Instant;

/** A market quote. */
public record Quote(String symbol, BigDecimal price, Instant at) {
}
