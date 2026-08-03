package com.example.bookstore.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public class CreateBookRequest {

    @NotBlank
    @Size(min = 1, max = 200)
    private String title;

    @NotNull
    private Genre genre;

    @Pattern(regexp = "^\\d{13}$")
    private String isbn;

    @Min(1450)
    @Max(2100)
    private Integer publicationYear;
}
