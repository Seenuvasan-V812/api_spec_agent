package com.example.inventory;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/** An inventory item. */
public class Item {

    private Long id;

    @NotBlank
    @Size(max = 120)
    private String name;

    @Min(0)
    private int quantity;

    private String warehouseCode;
}
